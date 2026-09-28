"""Persistent P-256 device identities and domain-separated signatures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import tempfile
from uuid import UUID

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)


class IdentityError(ValueError):
    """A persisted identity or supplied certificate is invalid."""


def _canonical_peer_id(peer_id: object) -> str:
    try:
        if not isinstance(peer_id, str) or str(UUID(peer_id)) != peer_id:
            raise ValueError("noncanonical UUID")
    except ValueError as error:
        raise IdentityError("peer_id must be a canonical UUID") from error
    return peer_id


def _certificate(certificate_der: bytes) -> x509.Certificate:
    if not isinstance(certificate_der, bytes):
        raise IdentityError("certificate DER must be bytes")
    try:
        return x509.load_der_x509_certificate(certificate_der)
    except ValueError as error:
        raise IdentityError("certificate DER is invalid") from error


def validate_certificate(certificate_der: bytes, peer_id: str) -> str:
    """Validate a P-256 certificate binding and return its SHA-256 fingerprint."""
    expected_peer_id = _canonical_peer_id(peer_id)
    certificate = _certificate(certificate_der)
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1 or common_names[0].value != expected_peer_id:
        raise IdentityError("certificate common name does not match peer_id")
    public_key = certificate.public_key()
    if (not isinstance(public_key, ec.EllipticCurvePublicKey)
            or not isinstance(public_key.curve, ec.SECP256R1)):
        raise IdentityError("certificate must use a P-256 public key")
    return certificate.fingerprint(hashes.SHA256()).hex()


def verify_signature(certificate_der: bytes, data: bytes,
                     signature: bytes) -> bool:
    """Verify an ECDSA SHA-256 signature with a P-256 certificate."""
    certificate = _certificate(certificate_der)
    public_key = certificate.public_key()
    if (not isinstance(public_key, ec.EllipticCurvePublicKey)
            or not isinstance(public_key.curve, ec.SECP256R1)):
        raise IdentityError("certificate must use a P-256 public key")
    if not isinstance(data, bytes):
        raise TypeError("signed data must be bytes")
    if not isinstance(signature, bytes):
        raise TypeError("signature must be bytes")
    try:
        public_key.verify(signature, data,
                          ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


class DeviceIdentity:
    """A peer-bound private key and self-signed certificate."""

    def __init__(self, peer_id: str, path: Path,
                 private_key: ec.EllipticCurvePrivateKey,
                 certificate: x509.Certificate) -> None:
        self.peer_id = peer_id
        self.path = path
        self._private_key = private_key
        self._certificate = certificate

    @classmethod
    def load_or_create(cls, path: Path | str, peer_id: str) -> DeviceIdentity:
        """Load one identity bundle, or atomically create it when absent."""
        canonical_id = _canonical_peer_id(peer_id)
        identity_path = Path(path)
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            bundle = identity_path.read_bytes()
        except FileNotFoundError:
            bundle = _new_bundle(canonical_id)
            if not _install_bundle(identity_path, bundle):
                bundle = identity_path.read_bytes()
        return cls._from_bundle(identity_path, canonical_id, bundle)

    @classmethod
    def _from_bundle(cls, path: Path, peer_id: str,
                     bundle: bytes) -> DeviceIdentity:
        private_pem, certificate_pem = _split_bundle(bundle)
        try:
            private_key = serialization.load_pem_private_key(
                private_pem, password=None)
            certificate = x509.load_pem_x509_certificate(certificate_pem)
        except (TypeError, ValueError) as error:
            raise IdentityError("identity bundle is corrupt") from error
        if (not isinstance(private_key, ec.EllipticCurvePrivateKey)
                or not isinstance(private_key.curve, ec.SECP256R1)):
            raise IdentityError("identity private key must use P-256")
        certificate_der = certificate.public_bytes(serialization.Encoding.DER)
        validate_certificate(certificate_der, peer_id)
        private_public = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        certificate_public = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        if private_public != certificate_public:
            raise IdentityError("identity private key and certificate do not match")
        _validate_self_signed_certificate(certificate)
        return cls(peer_id, path, private_key, certificate)

    @property
    def certificate_der(self) -> bytes:
        """Return the certificate in DER form."""
        return self._certificate.public_bytes(serialization.Encoding.DER)

    @property
    def certificate_pem(self) -> bytes:
        """Return the certificate in PEM form."""
        return self._certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def fingerprint(self) -> str:
        """Return the lowercase SHA-256 certificate fingerprint."""
        return self._certificate.fingerprint(hashes.SHA256()).hex()

    def sign(self, data: bytes) -> bytes:
        """Sign caller-provided domain-separated bytes."""
        if not isinstance(data, bytes):
            raise TypeError("signed data must be bytes")
        return self._private_key.sign(
            data, ec.ECDSA(hashes.SHA256()))


def _new_bundle(peer_id: str) -> bytes:
    private_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, peer_id)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=None, decipher_only=None), True)
        .add_extension(x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        ]), False)
        .sign(private_key, hashes.SHA256())
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    return private_pem + certificate.public_bytes(serialization.Encoding.PEM)


def _split_bundle(bundle: bytes) -> tuple[bytes, bytes]:
    if not isinstance(bundle, bytes) or not bundle.startswith(
            b"-----BEGIN PRIVATE KEY-----"):
        raise IdentityError("identity bundle is missing its private key")
    private_marker = b"-----END PRIVATE KEY-----"
    certificate_marker = b"-----END CERTIFICATE-----"
    private_end = bundle.find(private_marker)
    if private_end < 0:
        raise IdentityError("identity bundle has an incomplete private key")
    private_end += len(private_marker)
    separator_end = private_end
    while separator_end < len(bundle) and bundle[separator_end] in b"\r\n":
        separator_end += 1
    private_pem = bundle[:separator_end]
    certificate_pem = bundle[separator_end:]
    if (not certificate_pem.startswith(b"-----BEGIN CERTIFICATE-----")
            or certificate_pem.count(b"-----BEGIN CERTIFICATE-----") != 1):
        raise IdentityError("identity bundle is missing its certificate")
    certificate_end = certificate_pem.find(certificate_marker)
    if certificate_end < 0:
        raise IdentityError("identity bundle has an incomplete certificate")
    trailing = certificate_pem[certificate_end + len(certificate_marker):]
    if trailing.strip(b"\r\n"):
        raise IdentityError("identity bundle contains unexpected data")
    return private_pem, certificate_pem


def _validate_self_signed_certificate(certificate: x509.Certificate) -> None:
    if certificate.issuer != certificate.subject:
        raise IdentityError("identity certificate is not self-signed")
    public_key = certificate.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    try:
        public_key.verify(certificate.signature,
                          certificate.tbs_certificate_bytes,
                          ec.ECDSA(certificate.signature_hash_algorithm))
    except InvalidSignature as error:
        raise IdentityError("identity certificate signature is invalid") from error
    try:
        key_usage = certificate.extensions.get_extension_for_class(
            x509.KeyUsage).value
        extended_usage = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as error:
        raise IdentityError("identity certificate lacks TLS usage extensions") from error
    if not key_usage.digital_signature:
        raise IdentityError("identity certificate cannot sign")
    required = {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH}
    if not required.issubset(set(extended_usage)):
        raise IdentityError("identity certificate lacks TLS authentication usage")


def _install_bundle(path: Path, bundle: bytes) -> bool:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        try:
            os.chmod(temporary_path, 0o600)
        except OSError as error:
            logger.debug("Could not set identity file mode: %s", error)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False
        _fsync_parent(path)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError as error:
        logger.debug("Could not open identity directory for fsync: %s", error)
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        logger.debug("Could not fsync identity directory: %s", error)
    finally:
        os.close(descriptor)
