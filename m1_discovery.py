"""Run console-only LAN discovery without contacting Internet services."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import logging
import os
from pathlib import Path
import select
import time
from uuid import UUID, uuid4

from core.discovery import (
    ANNOUNCEMENT_INTERVAL, BROADCAST_ADDRESS, DISCOVERY_PORT,
    IPV6_CAPABILITY, TCP_PORT, DiscoveryTransport, Hello, encode_hello,
    ipv6_supported,
)


def load_identity(path: Path) -> str:
    """Read or exclusively create a persistent installation UUID."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="ascii") as stream:
            identifier = str(uuid4())
            stream.write(identifier + "\n")
            return identifier
    except FileExistsError:
        return str(UUID(path.read_text(encoding="ascii").strip()))


def port_number(value: str) -> int:
    """Validate a nonzero TCP or UDP port for argparse."""
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1–65535")
    return port


def run(hello: Hello, transport: DiscoveryTransport) -> None:
    """Announce on a monotonic schedule while receiving peer datagrams."""
    next_announcement = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= next_announcement:
            try:
                transport.announce(hello)
            except OSError as error:
                logging.warning("Announcement failed: %s", error)
            next_announcement = now + ANNOUNCEMENT_INTERVAL
        timeout = min(0.25, max(0.0, next_announcement - time.monotonic()))
        readable, _, _ = select.select(transport.receivers, [], [], timeout)
        for sock in readable:
            if sock is transport.receiver:
                result = transport.receive()
            else:
                result = transport.receive_v6()
            if result is not None:
                peer, address = result
                print(f"HELLO {peer.name!r} peer={peer.peer_id} "
                      f"session={peer.session_id} "
                      f"endpoint={address[0]}:{peer.tcp_port}", flush=True)


def main(runner: Callable[[Hello, DiscoveryTransport], None] = run) -> int:
    """Parse options and run discovery until interrupted."""
    root = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            if os.name == "nt" else
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=port_number, default=DISCOVERY_PORT)
    parser.add_argument("--tcp-port", type=port_number, default=TCP_PORT)
    parser.add_argument("--broadcast", default=BROADCAST_ADDRESS)
    parser.add_argument("--identity-file", type=Path,
                        default=root / "lan-manager" / "peer-id")
    parser.add_argument("--reuse-address", action="store_true",
                        help="opt in to OS-dependent same-host UDP sharing")
    parser.add_argument("--source-address", action="append", default=None,
                        help="also announce from one local IP address, with %%scope "
                             "for IPv6 link local; repeatable unless --no-fallback")
    parser.add_argument("--no-fallback", action="store_true",
                        help="do not also announce through the OS default route")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    transport = None
    try:
        capabilities = (IPV6_CAPABILITY,) if ipv6_supported() else ()
        hello = Hello(load_identity(args.identity_file), str(uuid4()),
                      args.name, args.tcp_port, capabilities)
        encode_hello(hello)
        sources = tuple(args.source_address) if args.source_address else None
        transport = DiscoveryTransport(hello.session_id, args.port,
                                       args.broadcast, args.reuse_address,
                                       sources, not args.no_fallback)
        print(f"Discovery listening on UDP {args.port}; session={hello.session_id}. "
              "TCP port is advertised only; M1 does not start a TCP service.",
              flush=True)
        runner(hello, transport)
    except KeyboardInterrupt:
        logging.info("Discovery stopped")
    except (OSError, ValueError) as error:
        logging.error("Discovery failed: %s", error)
        return 1
    finally:
        if transport is not None:
            transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
