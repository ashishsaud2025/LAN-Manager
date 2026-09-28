from __future__ import annotations

import base64
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from core.post_signatures import PostVerification, sign_post, verify_post
from core.posts import (
    MAX_POST_CERTIFICATE_DER,
    MAX_POST_SIGNATURE_DER,
    validate_post,
)
from core.storage import JsonLinesPostStore


class SigningIdentity:
    def __init__(self, author_id: str, *, rsa_key: bool = False) -> None:
        if rsa_key:
            self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, author_id)])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(self._key, hashes.SHA256())
        )
        self.certificate_der = certificate.public_bytes(serialization.Encoding.DER)
        self.fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        self.signed_data: bytes | None = None

    def sign(self, data: bytes) -> bytes:
        self.signed_data = data
        return self._key.sign(data, ec.ECDSA(hashes.SHA256()))


def _post(author_id: str | None = None) -> dict[str, object]:
    return {
        "post_id": str(uuid4()),
        "author_id": author_id or str(uuid4()),
        "text": "hello",
        "created_ms": 123456,
        "refs": [{"kind": "post", "id": str(uuid4())}],
    }


def test_legacy_post_remains_valid_and_is_unsigned() -> None:
    post = _post()

    assert validate_post(post) is post
    assert verify_post(post) == PostVerification("unsigned")


def test_unknown_fields_remain_legacy_unsigned_but_cannot_be_signed() -> None:
    post = _post()
    post["unsigned_extension"] = "mutable"

    assert validate_post(post) is post
    assert verify_post(post).state == "unsigned"
    with pytest.raises(ValueError, match="invalid fields"):
        sign_post(post, SigningIdentity(post["author_id"]))


def test_sign_and_verify_without_mutating_caller_values() -> None:
    post = _post()
    original = copy.deepcopy(post)
    identity = SigningIdentity(post["author_id"])

    signed = sign_post(post, identity)

    assert post == original
    assert signed is not post
    assert signed["refs"] is not post["refs"]
    assert signed["security"]["version"] == 1
    assert signed["security"]["algorithm"] == "ecdsa-p256-sha256"
    result = verify_post(signed)
    assert result == PostVerification("signed", identity.fingerprint)
    with pytest.raises(FrozenInstanceError):
        result.state = "invalid"


def test_unicode_payload_is_deterministic_and_verifiable() -> None:
    post = _post()
    post["text"] = "Héllo, LAN 世界"
    post["refs"] = [{"kind": "file", "id": "f", "name": "café.txt", "size": 7}]
    identity = SigningIdentity(post["author_id"])

    first = sign_post(post, identity)
    second = sign_post(post, identity)

    assert verify_post(first).state == "signed"
    assert verify_post(second).state == "signed"
    assert identity.signed_data == (
        b"LAN-MANAGER-POST-V1\x00"
        + (
            '{"author_id":"%s","created_ms":123456,"post_id":"%s",'
            '"refs":[{"id":"f","kind":"file","name":"caf\u00e9.txt",'
            '"size":7}],"text":"H\u00e9llo, LAN \u4e16\u754c"}'
            % (post["author_id"], post["post_id"])
        ).encode("utf-8")
    )


@pytest.mark.parametrize("field", ["post_id", "author_id", "text", "created_ms"])
def test_signed_scalar_field_tampering_is_detected(field: str) -> None:
    post = _post()
    signed = sign_post(post, SigningIdentity(post["author_id"]))
    if field in {"post_id", "author_id"}:
        signed[field] = str(uuid4())
    elif field == "text":
        signed[field] = "changed"
    else:
        signed[field] += 1

    assert verify_post(signed).state == "invalid"


def test_reference_tampering_is_detected() -> None:
    post = _post()
    signed = sign_post(post, SigningIdentity(post["author_id"]))

    signed["refs"][0]["id"] = str(uuid4())

    result = verify_post(signed)
    assert result.state == "invalid"
    assert result.reason == "post signature does not match"


@pytest.mark.parametrize(
    "security",
    [
        None,
        {},
        {"version": 1, "algorithm": "ecdsa-p256-sha256", "certificate": "%%", "signature": "AA=="},
        {"version": 1, "algorithm": "ecdsa-p256-sha256", "certificate": "AA==", "signature": "A===",},
        {"version": 2, "algorithm": "ecdsa-p256-sha256", "certificate": "AA==", "signature": "AA=="},
        {"version": 1, "algorithm": "other", "certificate": "AA==", "signature": "AA=="},
        {"version": 1, "algorithm": "ecdsa-p256-sha256", "certificate": "AA==", "signature": "AA==", "extra": 1},
    ],
)
def test_malformed_security_is_rejected_and_classified_invalid(
    security: object,
) -> None:
    post = _post()
    post["security"] = security

    with pytest.raises(ValueError):
        validate_post(post)
    assert verify_post(post).state == "invalid"


@pytest.mark.parametrize(
    ("field", "size"),
    [
        ("certificate", MAX_POST_CERTIFICATE_DER + 1),
        ("signature", MAX_POST_SIGNATURE_DER + 1),
    ],
)
def test_security_binary_fields_are_bounded(field: str, size: int) -> None:
    post = _post()
    security = {
        "version": 1,
        "algorithm": "ecdsa-p256-sha256",
        "certificate": "AA==",
        "signature": "AA==",
    }
    security[field] = base64.b64encode(b"x" * size).decode("ascii")
    post["security"] = security

    with pytest.raises(ValueError, match="exceeds allowed size"):
        validate_post(post)


def test_certificate_author_mismatch_is_invalid() -> None:
    post = _post()
    other_author = str(uuid4())
    identity = SigningIdentity(other_author)
    post["author_id"] = other_author
    signed = sign_post(post, identity)
    signed["author_id"] = str(uuid4())

    result = verify_post(signed)

    assert result.state == "invalid"
    assert result.fingerprint == identity.fingerprint
    assert result.reason == "post certificate subject CN must equal author_id"


def test_signing_rejects_certificate_for_another_author() -> None:
    post = _post()

    with pytest.raises(ValueError, match="subject CN"):
        sign_post(post, SigningIdentity(str(uuid4())))


def test_signing_rejects_non_p256_certificate() -> None:
    post = _post()

    with pytest.raises(ValueError, match="P-256"):
        sign_post(post, SigningIdentity(post["author_id"], rsa_key=True))


def test_invalid_der_certificate_and_signature_are_reported() -> None:
    post = _post()
    post["security"] = {
        "version": 1,
        "algorithm": "ecdsa-p256-sha256",
        "certificate": base64.b64encode(b"not DER").decode("ascii"),
        "signature": base64.b64encode(b"not DER").decode("ascii"),
    }

    validate_post(post)
    result = verify_post(post)
    assert result.state == "invalid"
    assert result.reason == "post certificate is not valid DER"


def test_store_rejects_tampered_signed_post(tmp_path: Path) -> None:
    post = _post()
    signed = sign_post(post, SigningIdentity(post["author_id"]))
    signed["text"] = "tampered after signing"
    store = JsonLinesPostStore(tmp_path / "posts.jsonl")

    with pytest.raises(ValueError, match="invalid signed post"):
        store.add(signed)

    assert store.count() == 0
