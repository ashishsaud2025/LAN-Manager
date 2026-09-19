from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from core.posts import page_posts, sort_key, validate_post
from core.storage import JsonLinesPostStore


def _post(author: str | None = None, text: str = "hello",
          refs: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"post_id": str(uuid4()), "author_id": author or str(uuid4()),
            "text": text, "created_ms": 1000, "refs": refs or []}


def test_validate_text_bounds() -> None:
    validate_post(_post(text="a"))
    with pytest.raises(ValueError):
        validate_post(_post(text="   "))
    with pytest.raises(ValueError):
        validate_post(_post(text="x" * 4097))


def test_validate_refs() -> None:
    good_file = {"kind": "file", "id": "x", "name": "a.bin", "size": 10}
    validate_post(_post(refs=[good_file]))
    good_post = {"kind": "post", "id": str(uuid4())}
    validate_post(_post(refs=[good_post]))
    with pytest.raises(ValueError):
        validate_post(_post(refs=[{"kind": "file", "id": "x"}]))
    with pytest.raises(ValueError):
        validate_post(_post(refs=[{"kind": "link", "id": "x"}]))
    with pytest.raises(ValueError):
        validate_post(_post(refs=[good_file] * 9))


def test_paging_stable_under_clock_skew() -> None:
    author_a, author_b = str(uuid4()), str(uuid4())
    early = _post(author_a, "first")
    early["created_ms"] = 99999
    late = _post(author_b, "second")
    late["created_ms"] = 1
    ordered = sorted([late, early], key=sort_key)
    first, cursor, done = page_posts(ordered, 1)
    assert not done and len(first) == 1
    second, _, done = page_posts(ordered, 1, cursor)
    assert done and len(second) == 1
    assert {first[0]["post_id"], second[0]["post_id"]} == {early["post_id"], late["post_id"]}


def test_author_filter_and_limits() -> None:
    author = str(uuid4())
    posts = [_post(author, f"n{i}") for i in range(3)] + [_post(text="other")]
    ordered = sorted(posts, key=sort_key)
    page, _, done = page_posts(ordered, 50, None, author)
    assert len(page) == 3 and done
    with pytest.raises(ValueError):
        page_posts(ordered, 0)
    with pytest.raises(ValueError):
        page_posts(ordered, 51)


def test_store_dedupe_and_restart(tmp_path: Path) -> None:
    store = JsonLinesPostStore(tmp_path / "posts.jsonl")
    post = _post()
    assert store.add(post) is True
    assert store.add(post) is False
    assert store.count() == 1
    again = JsonLinesPostStore(tmp_path / "posts.jsonl")
    assert again.count() == 1
    assert again.get(post["post_id"]) == post
    page, _, done = again.page(10)
    assert len(page) == 1 and done


def test_repeated_sync_without_duplicates(tmp_path: Path) -> None:
    first = JsonLinesPostStore(tmp_path / "a.jsonl")
    second = JsonLinesPostStore(tmp_path / "b.jsonl")
    post = _post()
    assert first.add(post) is True
    page, _, _ = first.page(10)
    for item in page:
        assert second.add(item) is True
    for item in page:
        assert second.add(item) is False
    assert second.count() == 1
