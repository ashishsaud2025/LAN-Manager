"""Serve and merge cached directory pages between peers."""

from __future__ import annotations

import json
from typing import Any

from core.protocol import MAX_MESSAGE_SIZE
from core.services import (
    DirectoryKind, LocalServiceDirectory, RemoteDirectoryCache,
    entry_dict, validate_directory_entry,
)

DIR_PAGE_BYTE_BUDGET = MAX_MESSAGE_SIZE - 4096


def validate_cursor(cursor: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a page cursor pointing at the last returned entry."""
    if cursor is None:
        return None
    if not isinstance(cursor, dict):
        raise ValueError("cursor must be an object or null")
    from uuid import UUID
    service_id = cursor.get("last_service")
    if not isinstance(service_id, str) or str(UUID(service_id)) != service_id:
        raise ValueError("invalid cursor last_service")
    return {"last_service": service_id}


def serve_query(directory: LocalServiceDirectory,
                body: dict[str, Any]) -> dict[str, Any]:
    """Answer one DIR_QUERY from local publications only, never cached remotes."""
    kind_value = body.get("kind")
    kind = None
    if kind_value is not None:
        kind = DirectoryKind(kind_value)
    entries = [entry_dict(entry)
               for entry in directory.snapshot(kind).entries]
    cursor = validate_cursor(body.get("cursor"))
    start = 0
    if cursor is not None:
        positions = [index for index, entry in enumerate(entries)
                     if entry["service_id"] == cursor["last_service"]]
        start = positions[-1] + 1 if positions else len(entries)
    page = entries[start:start + body["limit"]]
    bounded: list[dict[str, Any]] = []
    for entry in page:
        candidate = [*bounded, entry]
        encoded = json.dumps(
            candidate, ensure_ascii=False, allow_nan=False,
            separators=(",", ":")).encode("utf-8")
        if len(encoded) > DIR_PAGE_BYTE_BUDGET:
            break
        bounded.append(entry)
    if page and not bounded:
        raise ValueError("directory entry exceeds page byte budget")
    complete = len(bounded) == len(page) and start + len(page) >= len(entries)
    next_cursor = None
    if bounded:
        next_cursor = {"last_service": bounded[-1]["service_id"]}
    return {"entries": bounded, "next_cursor": next_cursor, "complete": complete}


def merge_page(cache: RemoteDirectoryCache,
               entries: list[dict[str, Any]]) -> tuple[int, int]:
    """Validate a full received page before merging any of its entries."""
    for raw in entries:
        validate_directory_entry(raw)
    return cache.merge(entries)
