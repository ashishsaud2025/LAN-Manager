"""Local post persistence behind a backend-neutral interface."""

from __future__ import annotations

from copy import deepcopy
import json
import os
import threading
from pathlib import Path
from typing import Any

from core.posts import page_posts, sort_key, validate_post

SCHEMA_VERSION = 1


class PostStore:
    """Backend contract so JSON can be replaced without changing sync logic."""

    def add(self, post: dict[str, Any]) -> bool:
        """Store one validated post; return False for a duplicate ID."""
        raise NotImplementedError

    def get(self, post_id: str) -> dict[str, Any] | None:
        """Return one post by ID, if present."""
        raise NotImplementedError

    def page(self, limit: int, cursor: dict[str, Any] | None = None,
             author_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
        """Return one stable page after the cursor with completion flag."""
        raise NotImplementedError

    def count(self) -> int:
        """Return the number of stored posts."""
        raise NotImplementedError


class JsonLinesPostStore(PostStore):
    """Append-only JSON Lines file with a schema header for future migration."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._posts: dict[str, dict[str, Any]] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._load()
        else:
            with path.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps({"schema": SCHEMA_VERSION}) + "\n")

    def _load(self) -> None:
        with self.path.open("r", encoding="utf-8") as stream:
            header = stream.readline()
            try:
                version = json.loads(header).get("schema")
            except ValueError as error:
                raise ValueError("post store header is not valid JSON") from error
            if version != SCHEMA_VERSION:
                raise ValueError(f"unsupported post store schema: {version}")
            for line in stream:
                if not line.strip():
                    continue
                post = validate_post(json.loads(line))
                self._posts.setdefault(post["post_id"], post)

    def add(self, post: dict[str, Any]) -> bool:
        """Validate, append, and fsync one post; duplicates report False."""
        validated = deepcopy(validate_post(post))
        with self._lock:
            if validated["post_id"] in self._posts:
                return False
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(validated, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._posts[validated["post_id"]] = validated
            return True

    def get(self, post_id: str) -> dict[str, Any] | None:
        with self._lock:
            post = self._posts.get(post_id)
            return deepcopy(post) if post is not None else None

    def page(self, limit: int, cursor: dict[str, Any] | None = None,
             author_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
        with self._lock:
            ordered = deepcopy(sorted(self._posts.values(), key=sort_key))
        return page_posts(ordered, limit, cursor, author_id)

    def count(self) -> int:
        with self._lock:
            return len(self._posts)
