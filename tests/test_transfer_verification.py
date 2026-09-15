from __future__ import annotations

import base64
import errno
import hashlib
import json
from pathlib import Path
from queue import Queue
import socket
import threading
from typing import Any
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.protocol import envelope, recv_message, send_message
from core.transfer import TransferError, TransferService


def test_publication_never_falls_back_to_overwriting(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "verified.part", tmp_path / "result.bin"
    source.write_bytes(b"verified bytes")

    def unsupported_link(*args: Any) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    def racing_replace(src: Path, dst: Path) -> None:
        target.write_bytes(b"another writer's file")
        raise AssertionError("no-clobber publication must not call os.replace")

    monkeypatch.setattr("core.transfer.os.link", unsupported_link)
    monkeypatch.setattr("core.transfer.os.replace", racing_replace)
    service = TransferService(Hello(str(uuid4()), str(uuid4()), "Receiver"),
                              lambda kind, value: True)
    with pytest.raises((OSError, TransferError)):
        service._publish(source, target)
    assert source.read_bytes() == b"verified bytes"
    assert not target.exists()


def test_transfer_fixtures_form_a_valid_transcript() -> None:
    fixture = Path(__file__).parents[1] / "protocol/fixtures/envelopes-v1.json"
    messages = json.loads(fixture.read_text(encoding="utf-8"))["valid"]
    offer = next(item["body"] for item in messages if item["type"] == "FILE_OFFER")
    result = next(item["body"] for item in messages if item["type"] == "FILE_RESULT")
    chunks = [item["body"] for item in messages if item["type"] == "FILE_CHUNK"]
    data = b""
    for chunk in chunks:
        assert chunk["transfer_id"] == offer["transfer_id"]
        assert chunk["offset"] == len(data)
        data += base64.b64decode(chunk["data"], validate=True)
    assert len(data) == offer["size"] == result["size"]
    assert hashlib.sha256(data).hexdigest() == offer["sha256"] == result["sha256"]


def test_chat_handoff_leaves_worker_free_and_cleans_up(tmp_path: Path) -> None:
    receiver = ChatService(Hello(str(uuid4()), str(uuid4()), "Receiver"))
    worker = threading.Thread(target=receiver._receive_worker, daemon=True)
    worker.start()
    sender_id, session = str(uuid4()), str(uuid4())
    transfer_id = str(uuid4())
    payload = b"hello\x00world" * 100
    digest = hashlib.sha256(payload).hexdigest()
    client, accepted = socket.socketpair()
    try:
        receiver._incoming.put(accepted)
        offer = envelope("FILE_OFFER", sender_id, session,
                         {"transfer_id": transfer_id, "name": "notes.bin",
                          "size": len(payload), "sha256": digest,
                          "to_session": receiver.hello.session_id})
        send_message(client, offer)
        kind, value = receiver.events.get(timeout=3)
        assert kind == "file_offer" and value["id"] == transfer_id

        chat_client, chat_accepted = socket.socketpair()
        with chat_client:
            receiver._incoming.put(chat_accepted)
            message = envelope("CHAT", sender_id, session,
                               {"scope": "room", "text": "chat while offer pending"})
            send_message(chat_client, message)
            assert recv_message(chat_client, timeout=2)["type"] == "ACK"

        target = tmp_path / "received.bin"
        assert receiver.transfers.decide(transfer_id, target)
        assert recv_message(client)["type"] == "FILE_ACCEPT"
        send_message(client, envelope("FILE_CHUNK", sender_id, session,
                                       {"transfer_id": transfer_id, "offset": 0,
                                        "data": base64.b64encode(payload).decode("ascii")}))
        send_message(client, envelope("FILE_DONE", sender_id, session,
                                       {"transfer_id": transfer_id}))
        assert recv_message(client)["type"] == "FILE_RESULT"
        assert target.read_bytes() == payload
    finally:
        client.close()
        receiver.stop()
        worker.join(3)
        assert receiver.transfers.join(3)
    assert not worker.is_alive()
    assert not list(tmp_path.glob(".lman-*.part"))


@pytest.mark.parametrize("failure", ["offset", "base64", "oversize", "overrun", "eof", "cancel"])
def test_incomplete_or_invalid_transfer_does_not_publish(tmp_path: Path, failure: str) -> None:
    receiver_hello = Hello(str(uuid4()), str(uuid4()), "Receiver")
    events: Queue[tuple[str, Any]] = Queue()

    def emit(kind: str, value: Any) -> bool:
        events.put((kind, value))
        return True

    service = TransferService(receiver_hello, emit)
    sender, session, identifier = str(uuid4()), str(uuid4()), str(uuid4())
    client, server = socket.socketpair()
    target = tmp_path / "received.bin"
    offer = envelope("FILE_OFFER", sender, session,
                     {"transfer_id": identifier, "name": "data.bin", "size": 3,
                      "sha256": hashlib.sha256(b"abc").hexdigest(),
                      "to_session": receiver_hello.session_id})
    try:
        service.receive(server, offer)
        assert events.get(timeout=2)[0] == "file_offer"
        assert service.decide(identifier, target)
        assert recv_message(client)["type"] == "FILE_ACCEPT"
        if failure == "eof":
            client.shutdown(socket.SHUT_WR)
        elif failure == "cancel":
            service.cancel(identifier)
        else:
            chunk = {"transfer_id": identifier, "offset": 0, "data": "YWJj"}
            if failure == "offset":
                chunk["offset"] = 1
            elif failure == "base64":
                chunk["data"] = "!!!!"
            elif failure == "oversize":
                chunk["data"] = base64.b64encode(bytes(65537)).decode("ascii")
            else:
                chunk["data"] = "YWJjZA=="
            send_message(client, envelope("FILE_CHUNK", sender, session, chunk))
        assert service.join(3)
        kind, status = events.get(timeout=2)
        assert kind == "transfer"
        assert status["state"] == ("cancelled" if failure == "cancel" else "failed")
        assert not target.exists()
        assert not list(tmp_path.glob(".lman-*.part"))
    finally:
        client.close()
        service.stop()
        assert service.join(3)
