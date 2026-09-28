"""Persistent peer certificate trust records."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from uuid import UUID

from core.identity import IdentityError, validate_certificate

SCHEMA_VERSION = 1
MAX_LABEL_LENGTH = 80
logger = logging.getLogger(__name__)


class TrustError(ValueError):
    """A trust operation or persisted trust document is invalid."""


class KeyChangedError(TrustError):
    """A paired peer presented a different certificate."""


@dataclass(frozen=True)
class TrustRecord:
    """One immutable peer certificate association."""

    peer_id: str
    certificate_der: bytes
    fingerprint: str
    label: str
    paired_ms: int


@dataclass(frozen=True)
class TrustSnapshot:
    """An immutable ordered view of the trust store."""

    schema: int
    records: tuple[TrustRecord, ...]


class TrustStore:
    """Thread-safe schema-one trust storage with atomic persistence."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records: dict[str, TrustRecord] = {}
        if self.path.exists():
            self._records = self._load()

    def pair(self, peer_id: str, certificate_der: bytes, label: str,
             paired_ms: int | None = None) -> TrustRecord:
        """Pair a peer, updating only the label when its key is unchanged."""
        canonical_id = _canonical_peer_id(peer_id)
        checked_label = _validate_label(label)
        fingerprint = _validated_fingerprint(certificate_der, canonical_id)
        timestamp = (_current_time_ms() if paired_ms is None
                     else _validate_paired_ms(paired_ms))
        with self._lock:
            existing = self._records.get(canonical_id)
            if existing is not None:
                if (existing.certificate_der != certificate_der
                        or existing.fingerprint != fingerprint):
                    raise KeyChangedError(
                        "paired peer presented a different certificate")
                if existing.label == checked_label:
                    return existing
                record = TrustRecord(canonical_id, certificate_der, fingerprint,
                                     checked_label, existing.paired_ms)
            else:
                record = TrustRecord(canonical_id, certificate_der, fingerprint,
                                     checked_label, timestamp)
            updated = dict(self._records)
            updated[canonical_id] = record
            self._persist(updated)
            self._records = updated
            return record

    def forget(self, peer_id: str) -> bool:
        """Forget a paired peer and report whether it was present."""
        canonical_id = _canonical_peer_id(peer_id)
        with self._lock:
            if canonical_id not in self._records:
                return False
            updated = dict(self._records)
            del updated[canonical_id]
            self._persist(updated)
            self._records = updated
            return True

    def get(self, peer_id: str) -> TrustRecord | None:
        """Return the immutable trust record for a peer, if present."""
        canonical_id = _canonical_peer_id(peer_id)
        with self._lock:
            return self._records.get(canonical_id)

    def snapshot(self) -> TrustSnapshot:
        """Return all records in stable peer ID order."""
        with self._lock:
            records = tuple(self._records[key] for key in sorted(self._records))
            return TrustSnapshot(SCHEMA_VERSION, records)

    def verify(self, peer_id: str, certificate_der: bytes) -> bool:
        """Return whether a peer presents its exactly paired certificate."""
        canonical_id = _canonical_peer_id(peer_id)
        fingerprint = _validated_fingerprint(certificate_der, canonical_id)
        with self._lock:
            record = self._records.get(canonical_id)
            return (record is not None
                    and record.certificate_der == certificate_der
                    and record.fingerprint == fingerprint)

    def _load(self) -> dict[str, TrustRecord]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TrustError("trust file is not valid UTF-8 JSON") from error
        if not isinstance(document, dict):
            raise TrustError("trust file must contain an object")
        if (type(document.get("schema")) is not int
                or document["schema"] != SCHEMA_VERSION):
            raise TrustError("unsupported trust file schema")
        if set(document) != {"schema", "records"}:
            raise TrustError("trust file has invalid fields")
        values = document.get("records")
        if not isinstance(values, dict):
            raise TrustError("trust records must be an object")
        records: dict[str, TrustRecord] = {}
        for peer_id, value in values.items():
            canonical_id = _canonical_peer_id(peer_id)
            records[canonical_id] = _decode_record(canonical_id, value)
        return records

    def _persist(self, records: dict[str, TrustRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": SCHEMA_VERSION,
            "records": {
                peer_id: {
                    "certificate_der": base64.b64encode(
                        record.certificate_der).decode("ascii"),
                    "fingerprint": record.fingerprint,
                    "label": record.label,
                    "paired_ms": record.paired_ms,
                }
                for peer_id, record in sorted(records.items())
            },
        }
        encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary_path = Path(temporary_name)
        try:
            try:
                os.chmod(temporary_path, 0o600)
            except OSError as error:
                logger.debug("Could not set trust file mode: %s", error)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            _fsync_parent(self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _canonical_peer_id(peer_id: object) -> str:
    try:
        if not isinstance(peer_id, str) or str(UUID(peer_id)) != peer_id:
            raise ValueError("noncanonical UUID")
    except ValueError as error:
        raise TrustError("peer_id must be a canonical UUID") from error
    return peer_id


def _validate_label(label: object) -> str:
    if (not isinstance(label, str) or not label.strip()
            or len(label) > MAX_LABEL_LENGTH):
        raise TrustError("label must contain 1 to 80 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in label):
        raise TrustError("label contains control characters")
    return label


def _validate_paired_ms(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2 ** 63 - 1:
        raise TrustError("paired_ms must be a nonnegative integer")
    return value


def _validated_fingerprint(certificate_der: bytes, peer_id: str) -> str:
    try:
        return validate_certificate(certificate_der, peer_id)
    except IdentityError as error:
        raise TrustError(str(error)) from error


def _decode_record(peer_id: str, value: Any) -> TrustRecord:
    if not isinstance(value, dict):
        raise TrustError("trust record must be an object")
    required = {"certificate_der", "fingerprint", "label", "paired_ms"}
    if set(value) != required:
        raise TrustError("trust record has invalid fields")
    encoded_certificate = value["certificate_der"]
    if not isinstance(encoded_certificate, str) or not encoded_certificate.isascii():
        raise TrustError("trust certificate must be base64 text")
    try:
        certificate_der = base64.b64decode(encoded_certificate, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise TrustError("trust certificate is not valid base64") from error
    if base64.b64encode(certificate_der).decode("ascii") != encoded_certificate:
        raise TrustError("trust certificate base64 is not canonical")
    fingerprint = _validated_fingerprint(certificate_der, peer_id)
    stored_fingerprint = value["fingerprint"]
    if (not isinstance(stored_fingerprint, str)
            or len(stored_fingerprint) != 64
            or any(character not in "0123456789abcdef"
                   for character in stored_fingerprint)
            or stored_fingerprint != fingerprint):
        raise TrustError("trust certificate fingerprint is invalid")
    label = _validate_label(value["label"])
    paired_ms = _validate_paired_ms(value["paired_ms"])
    return TrustRecord(peer_id, certificate_der, fingerprint, label, paired_ms)


def _current_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError as error:
        logger.debug("Could not open trust directory for fsync: %s", error)
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        logger.debug("Could not fsync trust directory: %s", error)
    finally:
        os.close(descriptor)
