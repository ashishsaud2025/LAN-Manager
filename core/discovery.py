"""Version-one discovery datagrams and raw UDP transport."""

from __future__ import annotations

import json
import logging
from ipaddress import IPv4Address, ip_address
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


def local_ipv4_addresses() -> tuple[str, ...]:
    """Return bounded interface candidates suitable for IPv4 LAN broadcast."""
    try:
        records = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as error:
        logger.warning("Could not enumerate local IPv4 addresses: %s", error)
        return ()
    addresses: set[str] = set()
    for record in records[:64]:
        value = record[4][0]
        try:
            address = ip_address(value)
        except ValueError:
            continue
        if (isinstance(address, IPv4Address) and not address.is_loopback
                and not address.is_link_local and not address.is_multicast
                and not address.is_unspecified):
            addresses.add(str(address))
    return tuple(sorted(addresses, key=lambda item: tuple(
        int(part) for part in item.split("."))))


class DiscoveryTransport:
    """Own a receiving socket and a separate broadcast sending socket."""

    def __init__(self, session_id: str, port: int = DISCOVERY_PORT,
                 destination: str = BROADCAST_ADDRESS,
                 reuse_address: bool = False,
                 source_addresses: tuple[str, ...] | None = None) -> None:
        self.session_id = session_id
        self.destination = (destination, port)
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.senders: list[socket.socket] = []
        try:
            if reuse_address:
                self.receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.receiver.bind((BIND_ADDRESS, port))
            self.senders.append(self._sender())
            candidates = (local_ipv4_addresses()
                          if source_addresses is None else source_addresses)
            for address in candidates[:16]:
                sender = self._sender()
                try:
                    sender.bind((address, 0))
                except OSError as error:
                    sender.close()
                    logger.warning("Could not bind discovery sender to %s: %s",
                                   address, error)
                    continue
                self.senders.append(sender)
        except OSError:
            self.close()
            raise

    @staticmethod
    def _sender() -> socket.socket:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return sender

    def announce(self, hello: Hello) -> None:
        """Send one HELLO; propagate failures to the runner."""
        if not self.senders:
            raise OSError("transport is closed")
        packet = encode_hello(hello)
        errors: list[OSError] = []
        delivered = 0
        for sender in self.senders:
            try:
                sender.sendto(packet, self.destination)
                delivered += 1
            except OSError as error:
                errors.append(error)
        if delivered == 0:
            raise errors[-1] if errors else OSError("transport is closed")
        for error in errors:
            logger.warning("Discovery announcement failed on one interface: %s", error)

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
        """Release all sockets, including after partial initialization."""
        self.receiver.close()
        for sender in self.senders:
            sender.close()
        self.senders.clear()
