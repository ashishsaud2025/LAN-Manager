from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
from unittest.mock import Mock
from uuid import uuid4

import pytest

from core.protocol import (
    FramingError, ProtocolError, encode_message, envelope, recv_message,
    send_message, validate_envelope,
)


class SplitSocket:
    """Socket double retaining unread bytes across arbitrarily small reads."""

    def __init__(self, data: bytes, chunk_size: int = 1) -> None:
        self.data = data
        self.chunk_size = chunk_size
        self.timeout = None

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def recv(self, size: int) -> bytes:
        count = min(size, self.chunk_size)
        result, self.data = self.data[:count], self.data[count:]
        return result


def test_shared_golden_vectors() -> None:
    path = Path(__file__).parents[1] / "protocol/fixtures/framing-v1.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    for vector in vectors["valid"]:
        frame = bytes.fromhex(vector["frame_hex"])
        assert encode_message(vector["object"]) == frame
        assert recv_message(SplitSocket(frame)) == vector["object"]
    for frame_hex in vectors["invalid_hex"]:
        with pytest.raises(ProtocolError):
            recv_message(SplitSocket(bytes.fromhex(frame_hex)))


def test_shared_envelope_vectors() -> None:
    path = Path(__file__).parents[1] / "protocol/fixtures/envelopes-v1.json"
    for message in json.loads(path.read_text(encoding="utf-8"))["valid"]:
        validate_envelope(message)
        assert recv_message(SplitSocket(encode_message(message))) == message


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 17, 65536])
def test_coalesced_frames_and_unicode(chunk_size: int) -> None:
    messages = [{"text": "नमस्ते"}, {"text": "x" * 10000}, {}]
    sock = SplitSocket(b"".join(encode_message(message) for message in messages), chunk_size)
    assert [recv_message(sock) for _ in messages] == messages
    assert recv_message(sock) is None
    assert sock.timeout is None


@pytest.mark.parametrize("payload", [b'[]', b'{"x":1,"x":2}', b'{"x":NaN}',
                                     b'{"x":1e999}', b'{"x":"\\ud800"}', b'\xff', b'{'])
def test_bad_json(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        recv_message(SplitSocket(struct.pack("!I", len(payload)) + payload))


def test_all_truncation_points() -> None:
    frame = encode_message({"text": "hello"})
    for length in range(1, len(frame)):
        with pytest.raises(FramingError):
            recv_message(SplitSocket(frame[:length]))


def test_deadline_and_write_timeout_restoration() -> None:
    with pytest.raises(TimeoutError):
        recv_message(SplitSocket(b""), timeout=0)
    sock = Mock()
    sock.gettimeout.return_value = 9
    sock.sendall.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        send_message(sock, {})
    sock.settimeout.assert_called_with(9)


@pytest.mark.parametrize("field,value", [("version", True), ("type", []),
                                       ("peer_id", "not-uuid"), ("body", [])])
def test_envelope_validation(field: str, value: object) -> None:
    message = envelope("ECHO", str(uuid4()), str(uuid4()), {})
    message[field] = value
    with pytest.raises(ProtocolError):
        validate_envelope(message)


def test_real_stream_roundtrip() -> None:
    sender, receiver = socket.socketpair()
    with sender, receiver:
        send_message(sender, {"text": "hello"})
        sender.shutdown(socket.SHUT_WR)
        assert recv_message(receiver) == {"text": "hello"}
        assert recv_message(receiver) is None
