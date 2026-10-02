"""Verify a clean install can run LAN Atlas without secrets or network."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

EXPECTED_RUNTIME = {
    "PySide6": "6.11.1",
    "cryptography": "46.0.5",
}
EXPECTED_TEST = {"pytest": "9.0.2"}
MINIMUM_PYTHON = (3, 11)
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer")
SECRET_NAMES = {"peer-id", "trust.json", "posts.jsonl", "id_rsa", "id_ed25519"}


def check_python() -> str | None:
    """Require a supported interpreter and report its exact version."""
    if sys.version_info < MINIMUM_PYTHON:
        return f"python {sys.version.split()[0]} below minimum 3.11"
    return None


def check_pins() -> str | None:
    """Require installed runtime distributions to match the release pins."""
    mismatched = []
    for name, pinned in EXPECTED_RUNTIME.items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            return f"{name} is not installed"
        if installed != pinned:
            mismatched.append(f"{name}=={installed} (want {pinned})")
    if mismatched:
        return "pin drift: " + ", ".join(mismatched)
    return None


def check_test_pins() -> str | None:
    """Pin pytest only when the test extra is installed."""
    try:
        installed = metadata.version("pytest")
    except metadata.PackageNotFoundError:
        return None
    if installed != EXPECTED_TEST["pytest"]:
        return f"pytest=={installed} (want {EXPECTED_TEST['pytest']})"
    return None


class _SplitSocket:
    """Replay one frame in small reads like a real TCP stream."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.timeout: float | None = None

    def recv(self, size: int) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout


def check_fixtures(root: Path) -> str | None:
    """Validate every shared wire fixture against the local codec."""
    from core.protocol import encode_message, recv_message, validate_envelope
    try:
        framing = json.loads((root / "protocol" / "fixtures"
                              / "framing-v1.json").read_text(encoding="utf-8"))
        envelopes = json.loads((root / "protocol" / "fixtures"
                                / "envelopes-v1.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return f"fixtures unreadable: {error}"
    try:
        for vector in framing["valid"]:
            frame = bytes.fromhex(vector["frame_hex"])
            if encode_message(vector["object"]) != frame:
                return "framing vector mismatch"
            if recv_message(_SplitSocket(frame)) != vector["object"]:
                return "framing round trip mismatch"
        from core.protocol import FramingError, ProtocolError
        for frame_hex in framing.get("invalid_hex", []):
            try:
                recv_message(_SplitSocket(bytes.fromhex(frame_hex)))
            except (FramingError, ProtocolError):
                continue
            return f"malformed frame accepted: {frame_hex[:16]}"
        for message in envelopes["valid"]:
            validate_envelope(message)
    except (KeyError, ValueError) as error:
        return f"fixture vector rejected: {error}"
    return None


def check_data_lifecycle() -> str | None:
    """Round-trip identity, trust, and post stores inside one temp dir."""
    from core.identity import DeviceIdentity
    from core.storage import JsonLinesPostStore
    from core.trust import TrustStore
    peer_id = "00000000-0000-4000-8000-000000000001"
    try:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            identity = DeviceIdentity.load_or_create(
                base / "identity.pem", peer_id)
            trust = TrustStore(base / "trust.json")
            trust.pair(peer_id, identity.certificate_der, "self")
            store = JsonLinesPostStore(base / "posts.jsonl")
            post = {"post_id": "00000000-0000-4000-8000-000000000002",
                    "author_id": peer_id, "text": "release probe",
                    "created_ms": 1, "refs": []}
            if not store.add(post) or store.get(post["post_id"]) is None:
                return "post store round trip failed"
            if not trust.verify(peer_id, identity.certificate_der):
                return "trust round trip failed"
    except (ValueError, OSError) as error:
        return f"data lifecycle failed: {error}"
    return None


def _is_secret_path(path: str) -> bool:
    """Match key material and live data files case insensitively."""
    lowered = path.lower()
    return (lowered.endswith(SECRET_SUFFIXES)
            or lowered.split("/")[-1] in SECRET_NAMES)


def check_no_tracked_secrets(root: Path) -> str | None:
    """Refuse release when key material or live data files are present."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True,
            timeout=30, encoding="utf-8", errors="replace")
        untracked = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "-z"], cwd=root,
            capture_output=True, timeout=30, encoding="utf-8",
            errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        return f"git inventory failed: {error}"
    for result, command in ((tracked, "ls-files"), (untracked, "status")):
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            return f"git {command} failed: {detail[-1] if detail else 'unknown'}"
    paths = [entry for entry in tracked.stdout.split("\0") if entry]
    paths.extend(entry[3:] for entry in untracked.stdout.split("\0")
                 if entry.startswith("?? "))
    banned = sorted({path for path in paths if _is_secret_path(path)})
    if banned:
        return "secret or data files present: " + ", ".join(banned[:5])
    return None


def run_checks(root: Path) -> list[str]:
    """Run every release gate and return human readable failures."""
    failures = []
    for label, check in (("python", check_python),
                         ("pins", check_pins),
                         ("test-pins", check_test_pins),
                         ("fixtures", lambda: check_fixtures(root)),
                         ("data", check_data_lifecycle),
                         ("secrets", lambda: check_no_tracked_secrets(root))):
        failure = check()
        print(f"[{'FAIL' if failure else 'ok'}] {label}"
              + (f": {failure}" if failure else ""))
        if failure:
            failures.append(f"{label}: {failure}")
    return failures


def main() -> int:
    """Check the release gates for the surrounding checkout."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    failures = run_checks(root)
    print(f"{len(failures)} failing gate(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
