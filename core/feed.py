"""Serve and merge cached feed pages between peers."""

from __future__ import annotations

from typing import Any

from core.posts import validate_post
from core.storage import PostStore


def serve_query(store: PostStore, body: dict[str, Any]) -> dict[str, Any]:
    """Answer one POST_QUERY from local storage, including cached re-shares."""
    posts, next_cursor, complete = store.page(
        body["limit"], body.get("cursor"), body.get("author_id"))
    return {"posts": posts, "next_cursor": next_cursor, "complete": complete}


def merge_page(store: PostStore, posts: list[dict[str, Any]]) -> tuple[int, int]:
    """Validate and store one received page; return added and duplicate counts."""
    added = duplicates = 0
    for post in posts:
        validate_post(post)
        if store.add(post):
            added += 1
        else:
            duplicates += 1
    return added, duplicates
