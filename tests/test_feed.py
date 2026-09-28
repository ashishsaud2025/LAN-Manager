from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.feed import merge_page, serve_query
from core.posts import validate_post
from core.protocol import ProtocolError, encode_message, envelope, validate_envelope
from core.storage import JsonLinesPostStore, PostStore


def _post(author: str | None = None, text: str = "hello") -> dict[str, object]:
    return {"post_id": str(uuid4()), "author_id": author or str(uuid4()),
            "text": text, "created_ms": 7, "refs": []}


def _ids(peer: str, session: str, body: dict[str, object], kind: str) -> dict[str, object]:
    return envelope(kind, peer, session, body)


def test_query_and_page_envelopes_validate() -> None:
    peer, session = str(uuid4()), str(uuid4())
    query = _ids(peer, session, {"cursor": None, "limit": 10, "author_id": None}, "POST_QUERY")
    validate_envelope(query)
    post = _post()
    page = envelope("POST_PAGE", peer, session, {"posts": [post], "next_cursor":
        {"last_author": post["author_id"], "last_post": post["post_id"]},
        "complete": True}, query["message_id"])
    validate_envelope(page)


def test_bad_query_rejected() -> None:
    peer, session = str(uuid4()), str(uuid4())
    with pytest.raises(ProtocolError):
        validate_envelope(_ids(peer, session, {"cursor": None, "limit": 0,
            "author_id": None}, "POST_QUERY"))
    with pytest.raises(ProtocolError):
        validate_envelope(envelope("POST_PAGE", peer, session,
            {"cursor": None, "limit": 10, "author_id": None}, str(uuid4())))


def test_serve_and_merge_roundtrip_with_reshare(tmp_path: Path) -> None:
    author_store = JsonLinesPostStore(tmp_path / "author.jsonl")
    holder_store = JsonLinesPostStore(tmp_path / "holder.jsonl")
    reader_store = JsonLinesPostStore(tmp_path / "reader.jsonl")
    posts = [_post(text=f"n{i}") for i in range(3)]
    for post in posts:
        assert author_store.add(post) is True
    first = serve_query(author_store, {"cursor": None, "limit": 2, "author_id": None})
    assert len(first["posts"]) == 2 and first["complete"] is False
    added, dupes = merge_page(holder_store, first["posts"])
    assert (added, dupes) == (2, 0)
    second = serve_query(author_store, {"cursor": first["next_cursor"],
        "limit": 2, "author_id": None})
    assert len(second["posts"]) == 1 and second["complete"] is True
    added, _ = merge_page(holder_store, second["posts"])
    assert added == 1
    reshare = serve_query(holder_store, {"cursor": None, "limit": 10, "author_id": None})
    assert len(reshare["posts"]) == 3
    added, _ = merge_page(reader_store, reshare["posts"])
    assert added == 3 and reader_store.count() == 3
    added, dupes = merge_page(reader_store, reshare["posts"])
    assert (added, dupes) == (0, 3)


def test_merge_rejects_bad_post(tmp_path: Path) -> None:
    store = JsonLinesPostStore(tmp_path / "s.jsonl")
    bad = _post()
    bad["text"] = "   "
    with pytest.raises(ValueError):
        merge_page(store, [bad])
    assert store.count() == 0


def test_empty_page_must_be_complete() -> None:
    peer, session = str(uuid4()), str(uuid4())
    with pytest.raises(ProtocolError):
        validate_envelope(envelope("POST_PAGE", peer, session, {"posts": [],
            "next_cursor": None, "complete": False}, str(uuid4())))


def test_signed_post_page_is_bounded_by_encoded_bytes() -> None:
    author = str(uuid4())
    certificate = base64.b64encode(b"x" * (16 * 1024)).decode("ascii")
    signature = base64.b64encode(b"y" * 256).decode("ascii")
    posts = []
    for index in range(50):
        post = _post(author, f"post {index}")
        post["security"] = {
            "version": 1,
            "algorithm": "ecdsa-p256-sha256",
            "certificate": certificate,
            "signature": signature,
        }
        posts.append(post)

    class LargePageStore(PostStore):
        def page(self, limit: int, cursor: dict[str, object] | None = None,
                 author_id: str | None = None) -> tuple[
                     list[dict[str, object]], dict[str, object], bool]:
            del limit, cursor, author_id
            last = posts[-1]
            return posts, {"last_author": last["author_id"],
                           "last_post": last["post_id"]}, True

    body = serve_query(
        LargePageStore(), {"cursor": None, "limit": 50, "author_id": None})
    assert 0 < len(body["posts"]) < 50
    assert body["complete"] is False
    assert body["next_cursor"]["last_post"] == body["posts"][-1]["post_id"]
    message = envelope(
        "POST_PAGE", str(uuid4()), str(uuid4()), body, str(uuid4()))
    assert len(encode_message(message)) < 1024 * 1024 + 4
    assert len(json.dumps(body).encode("utf-8")) < 1024 * 1024
