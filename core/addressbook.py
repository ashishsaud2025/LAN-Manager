"""Persisted manual dial entries for paired Internet peers."""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_LABEL_LENGTH = 80


@dataclass(frozen=True)
class AddressEntry:
    """One explicit dial target bound to a paired installation identity."""

    peer_id: str
    label: str
    host: str
    port: int
    capabilities: tuple[str, ...]
    fingerprint: str
    added_ms: int


class AddressBook:
    """Thread-safe schema-one address storage with atomic persistence."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: dict[str, AddressEntry] = {}
        if self.path.exists():
            self._entries = self._load()

    def add(self, peer_id: str, label: str, host: str, port: int,
            capabilities: tuple[str, ...] | list[str],
            fingerprint: str) -> AddressEntry:
        """Add or replace the dial entry for one paired peer."""
        entry = AddressEntry(
            _canonical_id(peer_id), _label(label), _host(host),
            _port(port), _capabilities(capabilities),
            _fingerprint(fingerprint), time.time_ns() // 1_000_000)
        with self._lock:
            updated = dict(self._entries)
            updated[entry.peer_id] = entry
            self._persist(updated)
            self._entries = updated
            return entry

    def remove(self, peer_id: str) -> bool:
        """Forget one dial entry and report whether it was present."""
        canonical = _canonical_id(peer_id)
        with self._lock:
            if canonical not in self._entries:
                return False
            updated = dict(self._entries)
            del updated[canonical]
            self._persist(updated)
            self._entries = updated
            return True

    def get(self, peer_id: str) -> AddressEntry | None:
        """Return one entry by exact peer ID."""
        with self._lock:
            return self._entries.get(_canonical_id(peer_id))

    def snapshot(self) -> tuple[AddressEntry, ...]:
        """Return entries ordered by label for stable presentation."""
        with self._lock:
            return tuple(sorted(self._entries.values(),
                                key=lambda item: (item.label.casefold(),
                                                  item.peer_id)))

    def _load(self) -> dict[str, AddressEntry]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("address book is not valid UTF-8 JSON") from error
        if not isinstance(document, dict):
            raise ValueError("address book must contain an object")
        if document.get("schema") != SCHEMA_VERSION:
            raise ValueError("unsupported address book schema")
        if set(document) != {"schema", "entries"}:
            raise ValueError("address book has invalid fields")
        values = document.get("entries")
        if not isinstance(values, dict):
            raise ValueError("address entries must be an object")
        entries: dict[str, AddressEntry] = {}
        for peer_id, raw in values.items():
            entry = _decode(peer_id, raw)
            entries[entry.peer_id] = entry
        return entries

    def _persist(self, entries: dict[str, AddressEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": SCHEMA_VERSION,
            "entries": {
                peer_id: {
                    "label": entry.label,
                    "host": entry.host,
                    "port": entry.port,
                    "capabilities": list(entry.capabilities),
                    "fingerprint": entry.fingerprint,
                    "added_ms": entry.added_ms,
                }
                for peer_id, entry in sorted(entries.items())
            },
        }
        encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _decode(peer_id: str, raw: Any) -> AddressEntry:
    key = _canonical_id(peer_id)
    if not isinstance(raw, dict):
        raise ValueError("address entry must be an object")
    expected = {"label", "host", "port", "capabilities", "fingerprint",
                "added_ms"}
    if set(raw) != expected:
        raise ValueError("address entry has invalid fields")
    added_ms = raw["added_ms"]
    if type(added_ms) is not int or not 0 <= added_ms <= 2 ** 63 - 1:
        raise ValueError("added_ms must be a nonnegative integer")
    return AddressEntry(
        key, _label(raw["label"]), _host(raw["host"]), _port(raw["port"]),
        _capabilities(raw["capabilities"]), _fingerprint(raw["fingerprint"]),
        added_ms)


def _canonical_id(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as error:
        raise ValueError("peer_id must be a canonical UUID") from error
    assert isinstance(value, str)
    return value


def _label(value: object) -> str:
    if (not isinstance(value, str) or not value.strip()
            or len(value) > MAX_LABEL_LENGTH):
        raise ValueError("label must contain 1 to 80 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("label contains control characters")
    return value.strip()


def _host(value: object) -> str:
    if (not isinstance(value, str) or not value.strip()
            or len(value) > 255):
        raise ValueError("host must be a bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("host contains control characters")
    return value.strip()


def _port(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError("port must be from 1 through 65535")
    return value


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or len(value) > 16:
        raise ValueError("capabilities must be a bounded list")
    for item in value:
        if (not isinstance(item, str) or not item or len(item) > 64
                or not item.isascii()
                or not all(c.isalnum() or c in "_-" for c in item)):
            raise ValueError("invalid capability")
    return tuple(value)


def _fingerprint(value: object) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError("fingerprint must be 64 lowercase hex characters")
    return value
