"""Immutable post model with stable paging independent of wall clocks."""

from __future__ import annotations

from typing import Any
from uuid import UUID

MAX_POST_TEXT = 4096
MAX_REFS = 8
MAX_REF_NAME = 255
PAGE_LIMIT_MAX = 50


def _check_uuid(value: object, name: str) -> str:
    """Require a canonical UUID string so IDs compare and dedupe reliably."""
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError(f"invalid {name}")
    return value


def validate_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """Validate one attachment reference without fetching any bytes."""
    if not isinstance(ref, dict):
        raise ValueError("reference must be an object")
    kind = ref.get("kind")
    if kind not in {"file", "post"}:
        raise ValueError("reference kind must be file or post")
    if kind == "post":
        _check_uuid(ref.get("id"), "post reference id")
    else:
        name = ref.get("name")
        if (not isinstance(name, str) or not name.strip() or len(name) > MAX_REF_NAME
                or name in {".", ".."} or any(c in name for c in "/\\:")
                or any(ord(c) < 32 for c in name)):
            raise ValueError("invalid file reference name")
        size = ref.get("size")
        if type(size) is not int or not 0 <= size <= 1024 * 1024 * 1024:
            raise ValueError("file reference size outside allowed range")
        digest = ref.get("sha256")
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("invalid file reference SHA-256")
    return ref


def validate_post(post: dict[str, Any]) -> dict[str, Any]:
    """Validate an immutable post dict before storage or sync."""
    if not isinstance(post, dict):
        raise ValueError("post must be an object")
    _check_uuid(post.get("post_id"), "post_id")
    _check_uuid(post.get("author_id"), "author_id")
    text = post.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_POST_TEXT:
        raise ValueError("post text must contain 1 to 4096 characters")
    created = post.get("created_ms")
    if type(created) is not int or not 0 <= created <= 2 ** 63 - 1:
        raise ValueError("post created_ms must be a nonnegative int")
    refs = post.get("refs", [])
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise ValueError("post refs must be a list of at most 8 items")
    for ref in refs:
        validate_ref(ref)
    return post


def sort_key(post: dict[str, Any]) -> tuple[str, str]:
    """Order by author then post ID so pages stay stable under clock skew."""
    return (post["author_id"], post["post_id"])


def validate_cursor(cursor: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a page cursor pointing at the last returned post."""
    if cursor is None:
        return None
    if not isinstance(cursor, dict):
        raise ValueError("cursor must be an object or null")
    _check_uuid(cursor.get("last_author"), "cursor last_author")
    _check_uuid(cursor.get("last_post"), "cursor last_post")
    return {"last_author": cursor["last_author"], "last_post": cursor["last_post"]}


def page_posts(sorted_posts: list[dict[str, Any]], limit: int,
               cursor: dict[str, Any] | None = None,
               author_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
    """Slice a sorted post list after the cursor with an optional author filter."""
    if type(limit) is not int or not 1 <= limit <= PAGE_LIMIT_MAX:
        raise ValueError("page limit must be 1 to 50")
    validate_cursor(cursor)
    if author_id is not None:
        _check_uuid(author_id, "author_id")
    start = None
    if cursor is not None:
        start = (cursor["last_author"], cursor["last_post"])
    out: list[dict[str, Any]] = []
    for post in sorted_posts:
        if author_id is not None and post["author_id"] != author_id:
            continue
        if start is not None and sort_key(post) <= start:
            continue
        out.append(post)
        if len(out) >= limit:
            break
    if not out:
        return [], cursor, True
    last = out[-1]
    next_cursor = {"last_author": last["author_id"], "last_post": last["post_id"]}
    remaining = False
    seen_last = False
    for post in sorted_posts:
        if author_id is not None and post["author_id"] != author_id:
            continue
        if not seen_last:
            if sort_key(post) == sort_key(last):
                seen_last = True
            continue
        remaining = True
        break
    return out, next_cursor, not remaining
