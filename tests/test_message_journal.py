from __future__ import annotations

from uuid import uuid4

import pytest

from core.discovery import Hello
from core.message_journal import MessageJournal
from core.protocol import envelope
from core.roster import Peer


def _hello(name: str) -> Hello:
    return Hello(str(uuid4()), str(uuid4()), name, 50001, ("chat_v1",))


def _peer(name: str) -> Peer:
    return Peer(_hello(name), "192.168.1.20", 1.0)


def test_outgoing_journal_tracks_each_recipient_without_reordering() -> None:
    moments = iter((1000, 1001, 1002))
    journal = MessageJournal(clock_ms=lambda: next(moments))
    sender = _hello("Sender")
    first, second = _peer("One"), _peer("Two")
    message = envelope("CHAT", sender.peer_id, sender.session_id,
                       {"scope": "room", "text": "hello"})
    record = journal.record_outgoing(message, sender, (first, second))
    assert record.recorded_ms == 1000
    assert [outcome.state for outcome in record.deliveries] == ["queued", "queued"]
    assert journal.update_delivery(message["message_id"], first.hello.session_id,
                                   "accepted", "accepted by receiving application")
    assert journal.update_delivery(message["message_id"], second.hello.session_id,
                                   "uncertain", "acknowledgement missing")
    snapshot = journal.snapshot()
    assert snapshot.revision == 3
    assert [outcome.state for outcome in snapshot.entries[0].deliveries] == [
        "accepted", "uncertain"]


def test_incoming_journal_deduplicates_and_evicts_at_bound() -> None:
    journal = MessageJournal(limit=2, clock_ms=lambda: 1)
    local = _hello("Local")
    sender = _hello("Sender")
    first = envelope("CHAT", sender.peer_id, sender.session_id,
                     {"scope": "room", "text": "first"})
    assert journal.record_incoming(first, sender.name, local)
    assert not journal.record_incoming(first, sender.name, local)
    for text in ("second", "third"):
        message = envelope("CHAT", sender.peer_id, sender.session_id,
                           {"scope": "room", "text": text})
        assert journal.record_incoming(message, sender.name, local)
    snapshot = journal.snapshot()
    assert [entry.text for entry in snapshot.entries] == ["second", "third"]
    assert all(entry.deliveries[0].state == "accepted"
               for entry in snapshot.entries)


def test_delivery_update_never_resurrects_evicted_message() -> None:
    journal = MessageJournal(limit=1, clock_ms=lambda: 1)
    sender = _hello("Sender")
    peer = _peer("Peer")
    first = envelope("CHAT", sender.peer_id, sender.session_id,
                     {"scope": "dm", "to_session": peer.hello.session_id,
                      "text": "first"})
    second = envelope("CHAT", sender.peer_id, sender.session_id,
                      {"scope": "dm", "to_session": peer.hello.session_id,
                       "text": "second"})
    journal.record_outgoing(first, sender, (peer,))
    journal.record_outgoing(second, sender, (peer,))
    assert not journal.update_delivery(
        first["message_id"], peer.hello.session_id, "accepted", "late")
    assert [entry.message_id for entry in journal.snapshot().entries] == [
        second["message_id"]]
    with pytest.raises(ValueError, match="delivery state"):
        journal.update_delivery(second["message_id"], peer.hello.session_id,
                                "read", "not implemented")


def test_journal_retains_authenticated_transport_evidence() -> None:
    journal = MessageJournal(clock_ms=lambda: 1)
    local = _hello("Local")
    sender = _hello("Sender")
    incoming = envelope("CHAT", sender.peer_id, sender.session_id,
                        {"scope": "room", "text": "protected"})
    assert journal.record_incoming(incoming, sender.name, local,
                                   authenticated=True)
    incoming_record = journal.snapshot().entries[0]
    assert incoming_record.authenticated
    assert incoming_record.deliveries[0].authenticated

    peer = _peer("Peer")
    outgoing = envelope("CHAT", local.peer_id, local.session_id,
                        {"scope": "room", "text": "outbound"})
    journal.record_outgoing(outgoing, local, (peer,))
    assert journal.update_delivery(
        outgoing["message_id"], peer.hello.session_id, "accepted",
        "authenticated acknowledgement", authenticated=True)
    outcome = journal.snapshot().entries[1].deliveries[0]
    assert outcome.authenticated
