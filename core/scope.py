"""Local versus Internet scope for connection policy decisions."""

from __future__ import annotations

import socket
from ipaddress import ip_address, ip_network

_LOCAL_NETWORKS = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("169.254.0.0/16"),
    ip_network("::1/128"),
    ip_network("fe80::/10"),
    ip_network("fc00::/7"),
)

RESOLVE_LIMIT = 16


def is_internet_host(host: str) -> bool:
    """Report whether a dial target leaves local network scope."""
    return any(
        _is_internet_address(ip_address(item.partition("%")[0]))
        for item in resolve_host(host))


def resolve_host(host: str) -> tuple[str, ...]:
    """Resolve a dial target to validated literal IP strings with scopes."""
    if not isinstance(host, str) or not host or len(host) > 255:
        raise ValueError("host must be a bounded string")
    base = host.partition("%")[0]
    try:
        parsed = ip_address(base)
    except ValueError:
        parsed = None
    if parsed is not None:
        return (host.strip(),)
    try:
        resolved = socket.getaddrinfo(base.strip(), None, socket.AF_UNSPEC,
                                      socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError(f"host does not resolve: {host}") from error
    addresses: list[str] = []
    for _family, _kind, _proto, _canon, sockaddr in resolved:
        try:
            ip_address(sockaddr[0])
        except ValueError:
            continue
        scope = sockaddr[3] if len(sockaddr) > 3 else 0
        labeled = f"{sockaddr[0]}%{scope}" if scope else sockaddr[0]
        if labeled not in addresses:
            addresses.append(labeled)
    if not addresses:
        raise ValueError(f"host has no usable addresses: {host}")
    return tuple(addresses)


def _is_internet_address(address: object) -> bool:
    """Treat only explicit local ranges as local; everything else is remote."""
    assert not isinstance(address, str)
    if any(address in network for network in _LOCAL_NETWORKS):
        return False
    if getattr(address, "is_unspecified", False):
        raise ValueError("unspecified address cannot be dialed")
    return True
