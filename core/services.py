"""Bounded local service and game publication directory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address, ip_address
import threading
from urllib.parse import quote
from uuid import uuid4

from core.discovery import Hello

DIRECTORY_LIMIT = 64
NAME_LIMIT = 80
DESCRIPTION_LIMIT = 500
PATH_LIMIT = 255


class DirectoryKind(Enum):
    """Locally published entry category."""

    SERVICE = "service"
    GAME = "game"


@dataclass(frozen=True)
class DirectoryEntry:
    """Validated local publication without inferred availability evidence."""

    service_id: str
    owner_peer_id: str
    owner_session_id: str
    owner_name: str
    kind: DirectoryKind
    name: str
    description: str
    scheme: str
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class DirectorySnapshot:
    """Immutable revisioned directory snapshot."""

    revision: int
    entries: tuple[DirectoryEntry, ...]


class LocalServiceDirectory:
    """Publish bounded session-local entries for desktop and portal views."""

    def __init__(self, owner: Hello, limit: int = DIRECTORY_LIMIT) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("directory limit must be a positive integer")
        self.owner = owner
        self.limit = limit
        self._entries: dict[str, DirectoryEntry] = {}
        self._revision = 0
        self._lock = threading.Lock()

    def register(self, kind: DirectoryKind | str, name: str, description: str,
                 scheme: str, host: str, port: int, path: str) -> DirectoryEntry:
        """Publish one validated local service or game for this session."""
        entry = self._entry(str(uuid4()), kind, name, description,
                            scheme, host, port, path)
        with self._lock:
            if len(self._entries) >= self.limit:
                raise RuntimeError("local directory is full")
            self._entries[entry.service_id] = entry
            self._revision += 1
        return entry

    def update(self, service_id: str, kind: DirectoryKind | str, name: str,
               description: str, scheme: str, host: str, port: int,
               path: str) -> DirectoryEntry:
        """Replace mutable fields while preserving publication identity."""
        entry = self._entry(service_id, kind, name, description,
                            scheme, host, port, path)
        with self._lock:
            if service_id not in self._entries:
                raise KeyError("unknown local directory entry")
            self._entries[service_id] = entry
            self._revision += 1
        return entry

    def withdraw(self, service_id: str) -> DirectoryEntry:
        """Remove one exact local publication."""
        with self._lock:
            try:
                entry = self._entries.pop(service_id)
            except KeyError as error:
                raise KeyError("unknown local directory entry") from error
            self._revision += 1
            return entry

    def get(self, service_id: str | None) -> DirectoryEntry | None:
        """Return one immutable local publication by identifier."""
        if service_id is None:
            return None
        with self._lock:
            return self._entries.get(service_id)

    def snapshot(self, kind: DirectoryKind | None = None) -> DirectorySnapshot:
        """Return deterministic entries, optionally filtered by category."""
        with self._lock:
            entries = tuple(self._entries.values())
            revision = self._revision
        filtered = (entries if kind is None else
                    tuple(entry for entry in entries if entry.kind is kind))
        ordered = tuple(sorted(filtered, key=lambda entry: (
            entry.kind.value, entry.name.casefold(), entry.service_id)))
        return DirectorySnapshot(revision, ordered)

    def _entry(self, service_id: str, kind: DirectoryKind | str, name: str,
               description: str, scheme: str, host: str, port: int,
               path: str) -> DirectoryEntry:
        parsed_kind = _kind(kind)
        clean_name = _text(name, "name", NAME_LIMIT, allow_empty=False)
        clean_description = _text(
            description, "description", DESCRIPTION_LIMIT, allow_empty=True)
        clean_scheme = scheme.strip().lower()
        if clean_scheme not in {"http", "https"}:
            raise ValueError("scheme must be http or https")
        clean_host = _host(host)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("port must be from 1 through 65535")
        clean_path = _path(path)
        return DirectoryEntry(
            service_id, self.owner.peer_id, self.owner.session_id,
            self.owner.name, parsed_kind, clean_name, clean_description,
            clean_scheme, clean_host, port, clean_path)


def browser_url(entry: DirectoryEntry) -> str:
    """Build one browser destination from validated structured fields."""
    host = _host(entry.host)
    scheme = entry.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("scheme must be http or https")
    if (not isinstance(entry.port, int) or isinstance(entry.port, bool)
            or not 1 <= entry.port <= 65535):
        raise ValueError("port must be from 1 through 65535")
    path = _path(entry.path)
    return f"{scheme}://{host}:{entry.port}{quote(path, safe='/-._~')}"


def _kind(value: DirectoryKind | str) -> DirectoryKind:
    if isinstance(value, DirectoryKind):
        return value
    try:
        return DirectoryKind(value)
    except ValueError as error:
        raise ValueError("kind must be service or game") from error


def _text(value: str, label: str, limit: int, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    clean = value.strip()
    if not allow_empty and not clean:
        raise ValueError(f"{label} must not be blank")
    if len(clean) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in clean):
        raise ValueError(f"{label} contains control characters")
    return clean


def _host(value: str) -> str:
    try:
        parsed = ip_address(value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError("host must be a concrete IPv4 address") from error
    if (not isinstance(parsed, IPv4Address) or parsed.is_unspecified
            or parsed.is_loopback or parsed.is_link_local
            or parsed.is_multicast or parsed.is_reserved):
        raise ValueError("host must be a concrete non-loopback IPv4 address")
    return str(parsed)


def _path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("path must start with /")
    if len(value) > PATH_LIMIT:
        raise ValueError(f"path exceeds {PATH_LIMIT} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("path contains control characters")
    if any(char in value for char in ("\\", "?", "#")):
        raise ValueError("path must not contain backslash, query, or fragment")
    return value
