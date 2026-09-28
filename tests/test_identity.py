from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from core.identity import DeviceIdentity, IdentityError, verify_signature


def test_identity_persists_across_restart(tmp_path: Path) -> None:
    peer_id = str(uuid4())
    path = tmp_path / "device.pem"
    first = DeviceIdentity.load_or_create(path, peer_id)
    second = DeviceIdentity.load_or_create(path, peer_id)

    assert second.certificate_der == first.certificate_der
    assert second.certificate_pem == first.certificate_pem
    assert second.fingerprint == first.fingerprint
    assert len(first.fingerprint) == 64
    assert set(first.fingerprint) <= set("0123456789abcdef")
    bundle = path.read_bytes()
    assert bundle.count(b"BEGIN PRIVATE KEY") == 1
    assert bundle.count(b"BEGIN CERTIFICATE") == 1
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_certificate_has_peer_and_tls_usage(tmp_path: Path) -> None:
    peer_id = str(uuid4())
    identity = DeviceIdentity.load_or_create(tmp_path / "device.pem", peer_id)
    certificate = x509.load_der_x509_certificate(identity.certificate_der)

    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert [attribute.value for attribute in common_names] == [peer_id]
    key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    extended_usage = certificate.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage).value
    assert key_usage.digital_signature
    assert ExtendedKeyUsageOID.SERVER_AUTH in extended_usage
    assert ExtendedKeyUsageOID.CLIENT_AUTH in extended_usage
    assert certificate.not_valid_before_utc < datetime.now(timezone.utc)
    assert certificate.not_valid_after_utc > datetime.now(timezone.utc)
    assert identity.certificate_pem == certificate.public_bytes(
        serialization.Encoding.PEM)


def test_signature_verifies_and_rejects_tampering(tmp_path: Path) -> None:
    identity = DeviceIdentity.load_or_create(
        tmp_path / "device.pem", str(uuid4()))
    payload = b"LAN-MANAGER-CHAT-V1\x00hello"
    signature = identity.sign(payload)

    assert verify_signature(identity.certificate_der, payload, signature)
    assert not verify_signature(
        identity.certificate_der, payload + b"changed", signature)
    assert not verify_signature(
        identity.certificate_der, b"LAN-MANAGER-FILE-V1\x00hello", signature)
    changed_signature = signature[:-1] + bytes([signature[-1] ^ 1])
    assert not verify_signature(identity.certificate_der, payload,
                                changed_signature)


@pytest.mark.parametrize("bundle", [
    b"not PEM",
    b"-----BEGIN PRIVATE KEY-----\npartial",
    b"-----BEGIN CERTIFICATE-----\npartial",
])
def test_corrupt_bundle_fails_without_regeneration(
        tmp_path: Path, bundle: bytes) -> None:
    path = tmp_path / "device.pem"
    path.write_bytes(bundle)

    with pytest.raises(IdentityError):
        DeviceIdentity.load_or_create(path, str(uuid4()))
    assert path.read_bytes() == bundle


def test_identity_refuses_wrong_peer_binding(tmp_path: Path) -> None:
    path = tmp_path / "device.pem"
    DeviceIdentity.load_or_create(path, str(uuid4()))
    original = path.read_bytes()

    with pytest.raises(IdentityError, match="common name"):
        DeviceIdentity.load_or_create(path, str(uuid4()))
    assert path.read_bytes() == original


def test_identity_refuses_mismatched_private_key(tmp_path: Path) -> None:
    first = DeviceIdentity.load_or_create(
        tmp_path / "first.pem", str(uuid4()))
    second_path = tmp_path / "second.pem"
    second = DeviceIdentity.load_or_create(second_path, str(uuid4()))
    first_bundle = (tmp_path / "first.pem").read_bytes()
    private_end = first_bundle.index(b"-----END PRIVATE KEY-----")
    private_end += len(b"-----END PRIVATE KEY-----\n")
    second_bundle = second_path.read_bytes()
    certificate_start = second_bundle.index(b"-----BEGIN CERTIFICATE-----")
    second_path.write_bytes(
        first_bundle[:private_end] + second_bundle[certificate_start:])

    with pytest.raises(IdentityError, match="do not match"):
        DeviceIdentity.load_or_create(second_path, second.peer_id)
    assert first.fingerprint != second.fingerprint
