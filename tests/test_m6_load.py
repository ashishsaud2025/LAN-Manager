from __future__ import annotations

import argparse
import socket
import tempfile
from pathlib import Path

from tools.m6_load import percentiles, run


def _options(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "tcp_port": 50001,
        "chat_count": 2,
        "chat_concurrency": 2,
        "transfers": 1,
        "transfer_bytes": 1024,
        "duration": 20.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_percentiles_known_inputs() -> None:
    assert percentiles([10.0, 20.0, 30.0, 40.0]) == {50: 20.0, 95: 40.0, 100: 40.0}
    assert percentiles([5.0]) == {50: 5.0, 95: 5.0, 100: 5.0}
    assert percentiles([]) == {}


def test_closed_port_reports_failure(capsys: object) -> None:
    code = run(_options(tcp_port=_closed_port()))
    assert code != 0
    out = capsys.readouterr().out  # type: ignore[union-attr]
    assert "chat attempted" in out


def test_temp_files_removed(tmp_path: Path, monkeypatch: object) -> None:
    import tools.m6_load as harness

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(dir=str(tmp_path), *args, **kwargs)  # type: ignore[arg-type]
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)  # type: ignore[union-attr]
    assert harness.run(_options(tcp_port=_closed_port())) != 0
    assert created and all(not Path(path).exists() for path in created)
