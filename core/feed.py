"""Serve and merge cached feed pages between peers."""

from __future__ import annotations

import json
from typing import Any

from core.post_signatures import verify_post
from core.posts import validate_post
from core.protocol import MAX_MESSAGE_SIZE
from core.storage import PostStore

POST_PAGE_BYTE_BUDGET = MAX_MESSAGE_SIZE - 4096


def serve_query(store: PostStore, body: dict[str, Any]) -> dict[str, Any]:
    """Answer one POST_QUERY from local storage, including cached re-shares."""
    posts, next_cursor, complete = store.page(
        body["limit"], body.get("cursor"), body.get("author_id"))
    bounded: list[dict[str, Any]] = []
    for post in posts:
        candidate = [*bounded, post]
        encoded = json.dumps(
            candidate, ensure_ascii=False, allow_nan=False,
            separators=(",", ":")).encode("utf-8")
        if len(encoded) > POST_PAGE_BYTE_BUDGET:
            break
        bounded.append(post)
    if bounded:
        last = bounded[-1]
        next_cursor = {
            "last_author": last["author_id"], "last_post": last["post_id"]}
    return {
        "posts": bounded,
        "next_cursor": next_cursor,
        "complete": complete and len(bounded) == len(posts),
    }


def merge_page(store: PostStore, posts: list[dict[str, Any]]) -> tuple[int, int]:
    """Validate and store one received page; return added and duplicate counts."""
    added = duplicates = 0
    for post in posts:
        validate_post(post)
        verification = verify_post(post)
        if verification.state == "invalid":
            raise ValueError(f"invalid signed post: {verification.reason}")
        if store.add(post):
            added += 1
        else:
            duplicates += 1
    return added, duplicates
