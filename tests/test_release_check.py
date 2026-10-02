from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.release_check import (
    _is_secret_path,
    check_fixtures,
    check_no_tracked_secrets,
    check_pins,
    check_test_pins,
    run_checks,
)


def test_release_gates_pass_on_verified_checkout() -> None:
    root = Path(__file__).parents[1]
    assert run_checks(root) == []


def test_pin_drift_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "version", lambda _name: "0.0.0")
    assert check_pins() is not None
    assert check_test_pins() is not None


def test_missing_runtime_distribution_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def absent(name: str) -> str:
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, "version", absent)
    assert check_pins() == "PySide6 is not installed"
    assert check_test_pins() is None


def test_corrupt_fixture_fails(tmp_path: Path) -> None:
    fixtures = tmp_path / "protocol" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "framing-v1.json").write_text("{}", encoding="utf-8")
    (fixtures / "envelopes-v1.json").write_text(
        json.dumps({"valid": [{"version": 2}]}), encoding="utf-8")
    assert check_fixtures(tmp_path) is not None


def test_git_failure_fails(monkeypatch: pytest.MonkeyPatch,
                           tmp_path: Path) -> None:
    failed = Mock(returncode=1, stdout="", stderr="fatal: bad repo")
    monkeypatch.setattr("tools.release_check.subprocess.run",
                        lambda *args, **kwargs: failed)
    assert "git ls-files failed" in (check_no_tracked_secrets(tmp_path) or "")


def test_secret_paths_detected() -> None:
    assert _is_secret_path("keys/identity.PEM")
    assert _is_secret_path("data/trust.json")
    assert _is_secret_path("id_rsa")
    assert not _is_secret_path("core/trust.py")
    assert not _is_secret_path("notes/m11-release.md")
