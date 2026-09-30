"""Version-one discovery datagrams and raw UDP transport."""

from __future__ import annotations

import json
import logging
import struct
from ipaddress import IPv4Address, IPv6Address, ip_address
import socket
from dataclasses import dataclass
from uuid import UUID

BIND_ADDRESS = "0.0.0.0"
BIND_ADDRESS_V6 = "::"
BROADCAST_ADDRESS = "255.255.255.255"
MULTICAST_ADDRESS_V6 = "ff02::1"
DISCOVERY_PORT = 50000
TCP_PORT = 50001
ANNOUNCEMENT_INTERVAL = 2.0
MAX_DATAGRAM_SIZE = 1200
HEADER = b"LMAN\x01"
# Advisory host stack hint, not proof of an active IPv6 receiver.
IPV6_CAPABILITY = "ipv6_v1"
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
    secure_port: int | None = None
    certificate_sha256: str | None = None


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
    has_secure_port = "secure_port" in value
    has_certificate = "certificate_sha256" in value
    if has_secure_port != has_certificate:
        raise DiscoveryError("incomplete secure transport metadata")
    has_capability = "secure_transport_v1" in capabilities
    if has_capability != has_secure_port:
        raise DiscoveryError(
            "secure transport capability and metadata must appear together")
    secure_port = value.get("secure_port")
    certificate_sha256 = value.get("certificate_sha256")
    if has_secure_port:
        if type(secure_port) is not int or not 1 <= secure_port <= 65535:
            raise DiscoveryError("invalid secure port")
        if (not isinstance(certificate_sha256, str)
                or len(certificate_sha256) != 64
                or any(character not in "0123456789abcdef"
                       for character in certificate_sha256)):
            raise DiscoveryError("invalid certificate fingerprint")
    return Hello(value["peer_id"], value["session_id"], name, port,
                 tuple(capabilities), secure_port, certificate_sha256)


def encode_hello(hello: Hello) -> bytes:
    """Encode a validated HELLO into one bounded UDP datagram."""
    value = {"version": 1, "peer_id": hello.peer_id,
             "session_id": hello.session_id, "name": hello.name,
             "tcp_port": hello.tcp_port, "capabilities": list(hello.capabilities)}
    if hello.secure_port is not None:
        value["secure_port"] = hello.secure_port
    if hello.certificate_sha256 is not None:
        value["certificate_sha256"] = hello.certificate_sha256
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


def _split_scope(value: str) -> tuple[str, int]:
    """Split an address with optional percent scope into base and scope ID."""
    base, separator, scope = value.partition("%")
    if not separator:
        return value, 0
    if scope.isdigit():
        return base, int(scope)
    try:
        return base, socket.if_nametoindex(scope)
    except (OSError, ValueError):
        return base, 0


def _normalize_scope(value: str) -> str:
    """Return a stable numeric scope key for sender reconciliation."""
    base, scope = _split_scope(value)
    return f"{base}%{scope}" if scope else base


def _format_scoped(base: str, scope_id: int) -> str:
    """Attach numeric scope context so link local endpoints stay routable."""
    if not scope_id:
        return base
    return f"{base}%{scope_id}"


def _is_ipv4(value: str) -> bool:
    """Classify an optionally scoped address without resolving scopes."""
    base, _scope = _split_scope(value)
    try:
        return isinstance(ip_address(base), IPv4Address)
    except ValueError:
        return False


def local_ipv6_addresses() -> tuple[str, ...]:
    """Return bounded IPv6 candidates including link local with scopes."""
    try:
        records = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError as error:
        logger.warning("Could not enumerate local IPv6 addresses: %s", error)
        return ()
    found: dict[str, tuple[int, int]] = {}
    for record in records[:64]:
        sockaddr = record[4]
        base = sockaddr[0]
        scope_id = sockaddr[3] if len(sockaddr) > 3 else 0
        try:
            address = ip_address(base)
        except ValueError:
            continue
        if (not isinstance(address, IPv6Address) or address.is_loopback
                or address.is_multicast or address.is_unspecified):
            continue
        labeled = _format_scoped(base, scope_id)
        key = int(address)
        if labeled not in found or scope_id < found[labeled][1]:
            found[labeled] = (key, scope_id)
    ordered = sorted(found, key=lambda item: (found[item][0], found[item][1]))
    return tuple(ordered)


def ipv6_supported() -> bool:
    """Return whether this host can open an IPv6 UDP socket."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        return True
    finally:
        try:
            sock.close()
        except OSError:
            logger.debug("Could not close IPv6 probe socket", exc_info=True)


MAX_SENDERS = 16


def _validate_source_addresses(
        value: tuple[str, ...] | list[str] | None) -> tuple[str, ...] | None:
    """Validate explicit egress selection without probing the network."""
    if value is None:
        return None
    if not isinstance(value, (tuple, list)):
        raise ValueError("source_addresses must be a tuple or None")
    resolved = tuple(value)
    for item in resolved:
        if not isinstance(item, str) or not item or len(item) > 255:
            raise ValueError("source address must be a bounded string")
        base, _scope = _split_scope(item)
        try:
            ip_address(base)
        except ValueError as error:
            raise ValueError(f"invalid IP source address: {item}") from error
    return resolved


class DiscoveryTransport:
    """Own IPv4 broadcast and IPv6 multicast sockets with one wire format."""

    def __init__(self, session_id: str, port: int = DISCOVERY_PORT,
                 destination: str = BROADCAST_ADDRESS,
                 reuse_address: bool = False,
                 source_addresses: tuple[str, ...] | None = None,
                 include_fallback: bool = True,
                 enable_ipv6: bool = True) -> None:
        if type(include_fallback) is not bool:
            raise ValueError("include_fallback must be bool")
        if type(enable_ipv6) is not bool:
            raise ValueError("enable_ipv6 must be bool")
        resolved_sources = _validate_source_addresses(source_addresses)
        self.session_id = session_id
        self.destination = (destination, port)
        self.port = port
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.senders: list[socket.socket] = []
        self._fallback: socket.socket | None = None
        self._bound: dict[str, socket.socket] = {}
        self._receiver6: socket.socket | None = None
        self._senders6: list[socket.socket] = []
        self._senders6_keys: list[str] = []
        self._bound6: dict[str, socket.socket] = {}
        self._scopes6: dict[str, int] = {}
        self.source_addresses = resolved_sources
        self.include_fallback = include_fallback
        self.enable_ipv6 = enable_ipv6
        try:
            if reuse_address:
                self.receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.receiver.bind((BIND_ADDRESS, port))
            if enable_ipv6:
                self._setup_ipv6_receiver(port, reuse_address)
            candidates = self._resolve_candidates(resolved_sources)
            self.refresh_senders(candidates, include_fallback)
        except (OSError, ValueError):
            self.close()
            raise

    @staticmethod
    def _sender() -> socket.socket:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return sender

    @staticmethod
    def _sender6() -> socket.socket:
        sender = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sender.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
        except OSError:
            logger.debug("Could not set IPv6 multicast hops", exc_info=True)
        try:
            sender.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 1)
        except OSError:
            logger.debug("Could not enable IPv6 multicast loopback", exc_info=True)
        return sender

    @property
    def receiver6(self) -> socket.socket | None:
        """Return the IPv6 multicast receiver when one was established."""
        return self._receiver6

    @property
    def ipv6_available(self) -> bool:
        """Return whether IPv6 multicast discovery is active."""
        return self._receiver6 is not None

    @property
    def receivers(self) -> list[socket.socket]:
        """Return live receivers for select without exposing sender state."""
        if self._receiver6 is not None:
            return [self.receiver, self._receiver6]
        return [self.receiver]

    def _resolve_candidates(
            self, resolved_sources: tuple[str, ...] | None) -> tuple[str, ...]:
        """Combine IPv4 and IPv6 candidates while preserving IPv4 baseline."""
        if resolved_sources is not None:
            return resolved_sources
        ipv4 = local_ipv4_addresses()
        if not self.enable_ipv6:
            return ipv4
        return ipv4 + local_ipv6_addresses()

    def _setup_ipv6_receiver(self, port: int, reuse_address: bool) -> None:
        """Bind an IPv6 multicast receiver or leave IPv4-only operation."""
        try:
            receiver = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        except OSError as error:
            logger.warning("IPv6 discovery unavailable: %s", error)
            return
        try:
            try:
                receiver.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except (AttributeError, OSError):
                logger.debug("Could not enforce IPv6-only receiver", exc_info=True)
            if reuse_address:
                receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            receiver.bind((BIND_ADDRESS_V6, port))
            try:
                receiver.setsockopt(socket.IPPROTO_IPV6,
                                    socket.IPV6_MULTICAST_LOOP, 1)
            except OSError:
                logger.debug("Could not enable IPv6 loopback", exc_info=True)
            scopes = {scope for _, scope in
                      (_split_scope(item) for item in local_ipv6_addresses())}
            scopes.add(0)
            group = IPv6Address(MULTICAST_ADDRESS_V6).packed
            joined = 0
            for scope in sorted(scopes):
                try:
                    mreq = struct.pack("16sI", group, scope)
                    receiver.setsockopt(socket.IPPROTO_IPV6,
                                        socket.IPV6_JOIN_GROUP, mreq)
                    joined += 1
                except OSError as error:
                    logger.debug("Could not join IPv6 multicast on scope %s: %s",
                                 scope, error)
            if not joined:
                try:
                    receiver.close()
                except OSError:
                    logger.debug("Could not close IPv6 receiver", exc_info=True)
                logger.warning("IPv6 discovery unavailable: no multicast join")
                return
            self._receiver6 = receiver
        except OSError as error:
            try:
                receiver.close()
            except OSError:
                logger.debug("Could not close IPv6 receiver", exc_info=True)
            logger.warning("IPv6 discovery unavailable: %s", error)

    def announce(self, hello: Hello) -> None:
        """Send one HELLO over IPv4 broadcast and IPv6 multicast senders."""
        if not self.senders and not self._senders6:
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
        for key, sender in zip(self._senders6_keys, self._senders6):
            scope = self._scopes6.get(key, 0)
            try:
                sender.sendto(packet, (MULTICAST_ADDRESS_V6, self.port, 0, scope))
                delivered += 1
            except OSError as error:
                errors.append(error)
        if delivered == 0:
            raise errors[-1] if errors else OSError("transport is closed")
        for error in errors:
            logger.warning("Discovery announcement failed on one interface: %s", error)

    def _refresh_ipv6(self, desired: tuple[str, ...]) -> bool:
        """Reconcile scoped IPv6 multicast senders without a fallback route."""
        if not self.enable_ipv6:
            if self._bound6 or self._senders6:
                for sender in tuple(self._bound6.values()):
                    try:
                        sender.close()
                    except OSError:
                        logger.debug("Could not close IPv6 sender", exc_info=True)
                self._bound6.clear()
                self._scopes6.clear()
                self._senders6.clear()
                self._senders6_keys.clear()
                return True
            return False
        wanted = {_normalize_scope(item) for item in desired}
        changed = False
        for address in tuple(self._bound6):
            if address not in wanted:
                sock = self._bound6.pop(address)
                self._scopes6.pop(address, None)
                try:
                    sock.close()
                except OSError:
                    logger.debug("Could not close IPv6 sender", exc_info=True)
                changed = True
        for address in desired:
            key = _normalize_scope(address)
            if key in self._bound6:
                continue
            base, scope = _split_scope(address)
            if scope == 0 and "%" in address:
                logger.warning("Skipping IPv6 sender with unknown scope: %s",
                               address)
                continue
            try:
                sender: socket.socket | None = self._sender6()
            except OSError as error:
                logger.warning("Could not create IPv6 sender for %s: %s",
                               address, error)
                continue
            try:
                sender.bind((base, 0, 0, scope))
            except OSError as error:
                sender.close()
                logger.warning("Could not bind IPv6 sender to %s: %s",
                               address, error)
                continue
            self._bound6[key] = sender
            self._scopes6[key] = scope
            changed = True
        ordered = [key for key in (_normalize_scope(item) for item in desired)
                   if key in self._bound6]
        self._senders6 = [self._bound6[key] for key in ordered]
        self._senders6_keys = ordered
        return changed

    def receive(self) -> tuple[Hello, tuple[str, int]] | None:
        """Read one IPv4 datagram with its observed source endpoint."""
        # A full UDP-sized buffer avoids platform-specific truncation behavior.
        packet, address = self.receiver.recvfrom(65535)
        try:
            hello = decode_hello(packet)
        except DiscoveryError as error:
            logger.warning("Ignoring datagram from %s: %s", address, error)
            return None
        if hello.session_id == self.session_id:
            return None
        return hello, (address[0], address[1])

    def receive_v6(self) -> tuple[Hello, tuple[str, int]] | None:
        """Read one IPv6 datagram, preserving link local scope context."""
        if self._receiver6 is None:
            return None
        packet, address = self._receiver6.recvfrom(65535)
        try:
            hello = decode_hello(packet)
        except DiscoveryError as error:
            logger.warning("Ignoring datagram from %s: %s", address, error)
            return None
        if hello.session_id == self.session_id:
            return None
        host = address[0]
        scope_id = address[3] if len(address) > 3 else 0
        return hello, (_format_scoped(host, scope_id), address[1])

    def refresh_senders(self, addresses: tuple[str, ...],
                          include_fallback: bool) -> bool:
        """Reconcile IPv4 and IPv6 egress sockets without touching receivers."""
        if type(include_fallback) is not bool:
            raise ValueError("include_fallback must be bool")
        desired = tuple(dict.fromkeys(tuple(addresses)))[:MAX_SENDERS]
        desired_v4 = tuple(item for item in desired if _is_ipv4(item))
        desired_v6 = tuple(item for item in desired if not _is_ipv4(item))
        changed = self._refresh_ipv4(desired_v4, include_fallback)
        if self._refresh_ipv6(desired_v6):
            changed = True
        self.include_fallback = include_fallback
        return changed

    def _refresh_ipv4(self, desired: tuple[str, ...],
                      include_fallback: bool) -> bool:
        """Reconcile the IPv4 broadcast senders that form the baseline."""
        wanted = set(desired)
        changed = include_fallback != (self._fallback is not None)
        for address in tuple(self._bound):
            if address not in wanted:
                sock = self._bound.pop(address)
                try:
                    sock.close()
                except OSError:
                    logger.debug("Could not close removed sender", exc_info=True)
                changed = True
        if include_fallback and self._fallback is None:
            self._fallback = self._sender()
            changed = True
        if not include_fallback and self._fallback is not None:
            try:
                self._fallback.close()
            except OSError:
                logger.debug("Could not close fallback sender", exc_info=True)
            self._fallback = None
            changed = True
        for address in desired:
            if address in self._bound:
                continue
            sender = self._sender()
            try:
                sender.bind((address, 0))
            except OSError as error:
                sender.close()
                logger.warning("Could not bind discovery sender to %s: %s",
                               address, error)
                continue
            self._bound[address] = sender
            changed = True
        ordered: list[socket.socket] = []
        if self._fallback is not None:
            ordered.append(self._fallback)
        for address in desired:
            sock = self._bound.get(address)
            if sock is not None:
                ordered.append(sock)
        self.senders = ordered
        return changed

    def bound_addresses(self) -> tuple[str, ...]:
        """Return bound egress addresses across both families in order."""
        ipv4 = tuple(address for address, sock in self._bound.items()
                     if sock in self.senders)
        ipv6 = tuple(self._senders6_keys)
        return ipv4 + ipv6

    def close(self) -> None:
        """Release all sockets, including after partial initialization."""
        try:
            self.receiver.close()
        except OSError:
            logger.debug("Could not close receiver", exc_info=True)
        if self._fallback is not None:
            try:
                self._fallback.close()
            except OSError:
                logger.debug("Could not close fallback sender", exc_info=True)
            self._fallback = None
        for sender in tuple(self._bound.values()):
            try:
                sender.close()
            except OSError:
                logger.debug("Could not close bound sender", exc_info=True)
        self._bound.clear()
        self.senders.clear()
        if self._receiver6 is not None:
            try:
                self._receiver6.close()
            except OSError:
                logger.debug("Could not close IPv6 receiver", exc_info=True)
            self._receiver6 = None
        for sender in tuple(self._bound6.values()):
            try:
                sender.close()
            except OSError:
                logger.debug("Could not close IPv6 sender", exc_info=True)
        self._bound6.clear()
        self._scopes6.clear()
        self._senders6.clear()
        self._senders6_keys.clear()
