from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from core.discovery import Hello
from core.roster import PeerRoster
from m2_roster import run


def test_refresh_and_exact_expiry() -> None:
    roster = PeerRoster("self")
    peer = Hello("installation", "session", "Alice")
    assert roster.update(peer, "192.0.2.1", 0)
    assert not roster.update(peer, "192.0.2.1", 2)
    assert not roster.expire(7.99)
    assert roster.expire(8)[0].hello == peer
    assert roster.snapshot() == ()
    assert roster.update(peer, "192.0.2.1", 9)


def test_sessions_not_ip_or_installation_identify_entries() -> None:
    roster = PeerRoster("self")
    assert not roster.update(Hello("me", "self", "Me"), "192.0.2.1", 0)
    for session in ("old", "new"):
        assert roster.update(Hello("same", session, "Alice"), "192.0.2.1", 0)
    assert len(roster.snapshot()) == 2


def test_metadata_and_network_changes_replace_endpoint() -> None:
    roster = PeerRoster("self")
    old = Hello("installation", "session", "Alice")
    roster.update(old, "192.0.2.1", 0)
    snapshot = roster.snapshot()
    new = Hello("installation", "session", "Renamed", 51001, ("chat",))
    assert roster.update(new, "192.0.2.2", 1)
    assert roster.snapshot()[0].ip == "192.0.2.2"
    assert roster.snapshot()[0].hello == new
    assert snapshot[0].hello == old


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        PeerRoster("self", timeout)


def test_runner_expires_during_silence() -> None:
    transport = Mock()
    transport.receive.return_value = (Hello("other", "other", "Alice"),
                                      ("192.0.2.1", 1234))
    with patch("m2_roster.time.monotonic", side_effect=[0, 0, 0, 0, 6, 6]), \
            patch("m2_roster.select.select",
                  side_effect=[([transport.receiver], [], []), KeyboardInterrupt]), \
            patch("m2_roster.print_roster") as display:
        sizes = []
        display.side_effect = lambda roster: sizes.append(len(roster.snapshot()))
        with pytest.raises(KeyboardInterrupt):
            run(Hello("me", "self", "Me"), transport)
    assert sizes == [0, 1, 0]
