from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.identity import DeviceIdentity
from core.trust import KeyChangedError, TrustError, TrustStore


def _identity(tmp_path: Path, name: str, peer_id: str | None = None) -> DeviceIdentity:
    return DeviceIdentity.load_or_create(
        tmp_path / f"{name}.pem", peer_id or str(uuid4()))


def test_trust_persists_exact_certificate_without_private_key(
        tmp_path: Path) -> None:
    identity = _identity(tmp_path, "peer")
    path = tmp_path / "trust.json"
    record = TrustStore(path).pair(
        identity.peer_id, identity.certificate_der, "Laptop", 1234)

    restarted = TrustStore(path)
    assert restarted.get(identity.peer_id) == record
    assert restarted.verify(identity.peer_id, identity.certificate_der)
    assert restarted.snapshot().records == (record,)
    serialized = path.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in serialized
    with pytest.raises(FrozenInstanceError):
        record.label = "changed"  # type: ignore[misc]


def test_pair_is_idempotent_and_updates_only_label(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "peer")
    store = TrustStore(tmp_path / "trust.json")
    first = store.pair(identity.peer_id, identity.certificate_der, "First", 100)
    same = store.pair(identity.peer_id, identity.certificate_der, "First", 200)
    updated = store.pair(identity.peer_id, identity.certificate_der, "Renamed", 300)

    assert same is first
    assert updated.label == "Renamed"
    assert updated.paired_ms == 100
    assert len(store.snapshot().records) == 1


def test_pair_rejects_key_change_without_overwrite(tmp_path: Path) -> None:
    peer_id = str(uuid4())
    first = _identity(tmp_path, "first", peer_id)
    second = _identity(tmp_path, "second", peer_id)
    path = tmp_path / "trust.json"
    store = TrustStore(path)
    original = store.pair(peer_id, first.certificate_der, "Peer", 10)
    original_file = path.read_bytes()

    with pytest.raises(KeyChangedError):
        store.pair(peer_id, second.certificate_der, "Impostor", 20)
    assert store.get(peer_id) == original
    assert path.read_bytes() == original_file
    assert not store.verify(peer_id, second.certificate_der)


def test_forget_persists_and_missing_is_noop(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "peer")
    path = tmp_path / "trust.json"
    store = TrustStore(path)
    store.pair(identity.peer_id, identity.certificate_der, "Peer", 10)

    assert store.forget(identity.peer_id)
    assert not store.forget(identity.peer_id)
    assert store.get(identity.peer_id) is None
    assert TrustStore(path).snapshot().records == ()


def test_absent_store_starts_empty_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "trust.json"
    store = TrustStore(path)

    assert store.snapshot().records == ()
    assert not path.exists()


@pytest.mark.parametrize("document", [
    "not JSON",
    json.dumps({"schema": 2, "records": {}}),
    json.dumps({"schema": 1, "records": []}),
    json.dumps({"schema": 1, "records": {str(uuid4()): {
        "certificate_der": "not base64!",
        "fingerprint": "0" * 64,
        "label": "Peer",
        "paired_ms": 1,
    }}}),
])
def test_malformed_trust_file_fails_loudly(
        tmp_path: Path, document: str) -> None:
    path = tmp_path / "trust.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(TrustError):
        TrustStore(path)


def test_trust_rejects_wrong_certificate_binding_and_bad_labels(
        tmp_path: Path) -> None:
    identity = _identity(tmp_path, "peer")
    store = TrustStore(tmp_path / "trust.json")

    with pytest.raises(TrustError, match="common name"):
        store.pair(str(uuid4()), identity.certificate_der, "Peer")
    for label in ("", "   ", "x" * 81, "bad\nlabel"):
        with pytest.raises(TrustError):
            store.pair(identity.peer_id, identity.certificate_der, label)
