"""Version-one discovery datagrams and raw UDP transport."""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from uuid import UUID

BIND_ADDRESS = "0.0.0.0"
BROADCAST_ADDRESS = "255.255.255.255"
DISCOVERY_PORT = 50000
TCP_PORT = 50001
ANNOUNCEMENT_INTERVAL = 2.0
MAX_DATAGRAM_SIZE = 1200
HEADER = b"LMAN\x01"
logger = logging.getLogger(__name__)


class DiscoveryError(ValueError):
    """A discovery datagram violates the version-one protocol."""


@dataclass(frozen=True)
class Hello:
    """An installation and session's advertised discovery metadata."""

    peer_id: str
    session_id: str
    name: str
    tcp_port: int = TCP_PORT
    capabilities: tuple[str, ...] = ()


def _validate(value: object) -> Hello:
    if not isinstance(value, dict):
        raise DiscoveryError("HELLO must be an object")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise DiscoveryError("unsupported discovery version")
    for key in ("peer_id", "session_id"):
        identifier = value.get(key)
        try:
            if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                raise ValueError("UUID must use canonical lowercase spelling")
        except ValueError as error:
            raise DiscoveryError(f"invalid {key}") from error
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise DiscoveryError("name must contain 1–80 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise DiscoveryError("name contains control characters")
    port = value.get("tcp_port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise DiscoveryError("invalid TCP port")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or len(capabilities) > 16:
        raise DiscoveryError("capabilities must be a bounded list")
    if any(not isinstance(item, str) or not item or len(item) > 64
           or not item.isascii() or not all(c.isalnum() or c in "_-" for c in item)
           for item in capabilities):
        raise DiscoveryError("invalid capability")
    return Hello(value["peer_id"], value["session_id"], name, port,
                 tuple(capabilities))


def encode_hello(hello: Hello) -> bytes:
    """Encode a validated HELLO into one bounded UDP datagram."""
    value = {"version": 1, "peer_id": hello.peer_id,
             "session_id": hello.session_id, "name": hello.name,
             "tcp_port": hello.tcp_port, "capabilities": list(hello.capabilities)}
    _validate(value)
    try:
        packet = HEADER + json.dumps(value, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")
    except UnicodeError as error:
        raise DiscoveryError("invalid Unicode") from error
    if len(packet) > MAX_DATAGRAM_SIZE:
        raise DiscoveryError("discovery datagram too large")
    return packet


def decode_hello(packet: bytes) -> Hello:
    """Decode a HELLO, ignoring unknown optional JSON fields."""
    if len(packet) > MAX_DATAGRAM_SIZE or not packet.startswith(HEADER):
        raise DiscoveryError("invalid discovery header or size")
    try:
        value = json.loads(packet[len(HEADER):].decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DiscoveryError("invalid UTF-8 JSON") from error
    hello = _validate(value)
    try:
        hello.name.encode("utf-8")
    except UnicodeError as error:
        raise DiscoveryError("invalid Unicode name") from error
    return hello


class DiscoveryTransport:
    """Own a receiving socket and a separate broadcast sending socket."""

    def __init__(self, session_id: str, port: int = DISCOVERY_PORT,
                 destination: str = BROADCAST_ADDRESS,
                 reuse_address: bool = False) -> None:
        self.session_id = session_id
        self.destination = (destination, port)
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender: socket.socket | None = None
        try:
            if reuse_address:
                self.receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.receiver.bind((BIND_ADDRESS, port))
            self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            self.close()
            raise

    def announce(self, hello: Hello) -> None:
        """Send one HELLO; propagate failures to the runner."""
        if self.sender is None:
            raise OSError("transport is closed")
        self.sender.sendto(encode_hello(hello), self.destination)

    def receive(self) -> tuple[Hello, tuple[str, int]] | None:
        """Read one datagram, returning other sessions and their observed source."""
        # A full UDP-sized buffer avoids platform-specific truncation behavior.
        packet, address = self.receiver.recvfrom(65535)
        try:
            hello = decode_hello(packet)
        except DiscoveryError as error:
            logger.warning("Ignoring datagram from %s: %s", address, error)
            return None
        if hello.session_id == self.session_id:
            return None
        return hello, address

    def close(self) -> None:
        """Release both sockets, including after partial initialization."""
        self.receiver.close()
        if self.sender is not None:
            self.sender.close()
            self.sender = None
