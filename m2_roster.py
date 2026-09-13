"""Run live LAN presence with a six-second session timeout."""

from __future__ import annotations

import logging
import select
import time

from core.discovery import ANNOUNCEMENT_INTERVAL, DiscoveryTransport, Hello
from core.roster import PeerRoster
from m1_discovery import main


def print_roster(roster: PeerRoster) -> None:
    """Print active sessions only when roster contents change."""
    peers = roster.snapshot()
    print(f"Online sessions: {len(peers)}", flush=True)
    for peer in peers:
        print(f"  {peer.hello.name!r} peer={peer.hello.peer_id} "
              f"session={peer.hello.session_id} "
              f"endpoint={peer.ip}:{peer.hello.tcp_port}", flush=True)


def run(hello: Hello, transport: DiscoveryTransport) -> None:
    """Refresh and expire sessions even when no datagrams arrive."""
    roster = PeerRoster(hello.session_id)
    next_announcement = time.monotonic()
    print_roster(roster)
    while True:
        now = time.monotonic()
        changed = bool(roster.expire(now))
        if now >= next_announcement:
            try:
                transport.announce(hello)
            except OSError as error:
                logging.warning("Announcement failed: %s", error)
            next_announcement = now + ANNOUNCEMENT_INTERVAL
        if changed:
            print_roster(roster)
        timeout = min(0.25, max(0.0, next_announcement - time.monotonic()))
        readable, _, _ = select.select([transport.receiver], [], [], timeout)
        if readable:
            try:
                result = transport.receive()
            except OSError as error:
                logging.warning("Reception failed: %s", error)
                # A failed socket must not turn the event loop into a busy retry.
                time.sleep(0.25)
                continue
            if result is not None:
                peer, address = result
                if roster.update(peer, address[0], time.monotonic()):
                    print_roster(roster)


if __name__ == "__main__":
    raise SystemExit(main(run))
