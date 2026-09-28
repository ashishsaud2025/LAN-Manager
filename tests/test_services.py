from __future__ import annotations

from uuid import uuid4

import pytest

from core.discovery import Hello
from core.services import (DirectoryKind, LocalServiceDirectory, browser_url)


def _directory(limit: int = 64) -> LocalServiceDirectory:
    hello = Hello(str(uuid4()), str(uuid4()), "Owner", 50001, ())
    return LocalServiceDirectory(hello, limit)


def test_directory_register_update_filter_and_withdraw() -> None:
    directory = _directory()
    service = directory.register(
        "service", "Docs", "Local documentation", "http",
        "192.168.1.20", 8000, "/docs")
    game = directory.register(
        DirectoryKind.GAME, "Arena", "LAN lobby", "https",
        "192.168.1.21", 8443, "/lobby")
    assert browser_url(service) == "http://192.168.1.20:8000/docs"
    assert directory.snapshot(DirectoryKind.SERVICE).entries == (service,)
    assert directory.snapshot(DirectoryKind.GAME).entries == (game,)

    updated = directory.update(
        service.service_id, "service", "Docs v2", "Updated", "https",
        "192.168.1.20", 8443, "/docs/v2")
    assert updated.service_id == service.service_id
    assert updated.owner_peer_id == service.owner_peer_id
    assert directory.snapshot().revision == 3
    assert directory.withdraw(game.service_id) == game
    assert directory.snapshot().entries == (updated,)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "0.0.0.0", "169.254.1.2", "224.0.0.1", "::1", "localhost",
])
def test_directory_rejects_non_visitable_hosts(host: str) -> None:
    with pytest.raises(ValueError, match="IPv4"):
        _directory().register("service", "Bad", "", "http", host, 80, "/")


def test_directory_validates_url_parts_and_capacity() -> None:
    directory = _directory(limit=1)
    entry = directory.register(
        "service", "Unicode", "", "http", "10.0.0.5", 8080, "/café")
    assert browser_url(entry) == "http://10.0.0.5:8080/caf%C3%A9"
    with pytest.raises(RuntimeError, match="full"):
        directory.register("game", "Other", "", "http", "10.0.0.6", 80, "/")
    with pytest.raises(ValueError, match="scheme"):
        _directory().register(
            "service", "Shell", "", "ssh", "10.0.0.5", 22, "/")
    with pytest.raises(ValueError, match="query"):
        _directory().register(
            "service", "Query", "", "http", "10.0.0.5", 80, "/?x=1")
    with pytest.raises(KeyError, match="unknown"):
        directory.withdraw(str(uuid4()))
