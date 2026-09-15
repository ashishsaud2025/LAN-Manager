"""Drive chat and transfer load against a running peer and print measurements."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.discovery import Hello
from core.protocol import (
    ProtocolError,
    envelope,
    recv_message,
    send_message,
    validate_envelope,
)
from core.roster import Peer
from core.transfer import TransferService

CONNECT_TIMEOUT = 3.0
CHAT_TIMEOUT = 10.0
TRANSFER_CEILING = 300.0
MONITOR_INTERVAL = 0.02
TERMINAL_TRANSFER_STATES = {"verified", "failed", "cancelled", "declined"}


def percentiles(samples: list[float], keys: tuple[int, ...] = (50, 95, 100)) -> dict[int, float]:
    """Nearest-rank percentiles over a snapshot of latency samples."""
    ordered = sorted(samples)
    if not ordered:
        return {}
    result = {}
    for key in keys:
        rank = (key * len(ordered) + 99) // 100
        result[key] = ordered[min(max(rank - 1, 0), len(ordered) - 1)]
    return result


def _chat_attempt(host: str, port: int, peer_id: str, session_id: str,
                  text: str) -> tuple[bool, float, str, str]:
    """Send one CHAT on a fresh connection and report ACK, latency, and target."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as conn:
            request = envelope("CHAT", peer_id, session_id, {"scope": "room", "text": text})
            send_message(conn, request)
            reply = recv_message(conn, CHAT_TIMEOUT)
            if reply is None:
                return False, (time.perf_counter() - start) * 1000.0, "", "no acknowledgement"
            validate_envelope(reply)
            if reply["type"] != "ACK" or reply.get("reply_to") != request["message_id"]:
                return False, (time.perf_counter() - start) * 1000.0, "", "invalid acknowledgement"
            return True, (time.perf_counter() - start) * 1000.0, reply["peer_id"], reply["session_id"]
    except (OSError, ProtocolError, ValueError) as error:
        return False, (time.perf_counter() - start) * 1000.0, "", str(error)


def _print_table(title: str, rows: list[tuple[str, str]]) -> None:
    """Print a plain two-column table to stdout."""
    print(title)
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        print(f"{name:<{width}}  {value}")


def run(options: argparse.Namespace) -> int:
    """Execute the load plan and return 0 only if every attempt succeeded."""
    payload_dir = Path(tempfile.mkdtemp(prefix="m6-load-"))
    try:
        return _execute(options, payload_dir)
    finally:
        try:
            shutil.rmtree(payload_dir)
        except OSError as error:
            print(f"warning: cannot remove {payload_dir}: {error}")


def _execute(options: argparse.Namespace, payload_dir: Path) -> int:
    peer_id, session_id = str(uuid4()), str(uuid4())
    payloads = []
    for index in range(max(0, options.transfers)):
        path = payload_dir / f"payload-{index}.bin"
        with path.open("wb") as stream:
            stream.write(os.urandom(max(0, options.transfer_bytes)))
        payloads.append(path)
    deadline = None if options.duration is None else time.monotonic() + options.duration
    stop_monitor = threading.Event()
    peak = [threading.active_count()]

    def monitor() -> None:
        while not stop_monitor.wait(MONITOR_INTERVAL):
            count = threading.active_count()
            if count > peak[0]:
                peak[0] = count

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    hello = Hello(peer_id, session_id, "m6-load")
    transfer_events: list[tuple[str, Any]] = []
    events_lock = threading.Lock()

    def emit(kind: str, value: Any) -> bool:
        with events_lock:
            transfer_events.append((kind, value))
        return True

    service = TransferService(hello, emit)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, options.chat_concurrency))
    chat_ok = chat_failed = 0
    latencies: list[float] = []
    target: dict[str, str] = {}
    target_lock = threading.Lock()
    transfer_ok = transfer_failed = 0
    verified_bytes = 0
    cancelled = False
    wall_start = time.monotonic()
    transfer_start = 0.0
    try:
        futures = []
        for index in range(max(0, options.chat_count)):
            if deadline is not None and time.monotonic() >= deadline:
                chat_failed += max(0, options.chat_count) - index
                break
            text = f"load {index}"
            futures.append(pool.submit(_chat_attempt, options.host, options.tcp_port,
                                       peer_id, session_id, text))
        for future in concurrent.futures.as_completed(futures):
            ok, ms, target_peer, target_session = future.result()
            latencies.append(ms)
            if ok:
                chat_ok += 1
                with target_lock:
                    target.setdefault("peer_id", target_peer)
                    target.setdefault("session_id", target_session)
            else:
                chat_failed += 1
        if payloads:
            transfer_start = time.monotonic()
            if "peer_id" not in target:
                transfer_failed = len(payloads)
            else:
                peer = Peer(Hello(target["peer_id"], target["session_id"], options.host,
                                  options.tcp_port, ("file_v1",)),
                            options.host, time.monotonic())
                send_futures = []
                for path in payloads:
                    if deadline is not None and time.monotonic() >= deadline:
                        transfer_failed += 1
                        continue
                    send_futures.append((path, pool.submit(service.send, path, peer)))
                submitted: list[tuple[Path, str]] = []
                for path, future in send_futures:
                    try:
                        submitted.append((path, future.result()))
                    except (OSError, ValueError) as error:
                        transfer_failed += 1
                        print(f"transfer rejected: {error}")
                transfer_ids = [identifier for _, identifier in submitted]
                transfer_cap = deadline if deadline is not None \
                    else time.monotonic() + TRANSFER_CEILING
                done_ids, timed_out = _await_transfers(service, transfer_events,
                                                       transfer_ids, transfer_cap)
                transfer_failed += len(timed_out)
                done_set = set(done_ids)
                for path, identifier in submitted:
                    if identifier not in done_set:
                        continue
                    if _terminal_state(transfer_events, identifier) == "verified":
                        transfer_ok += 1
                        verified_bytes += path.stat().st_size
                    else:
                        transfer_failed += 1
    except KeyboardInterrupt:
        cancelled = True
    finally:
        pool.shutdown(cancel_futures=True)
        service.stop()
        service.join(5.0)
        stop_monitor.set()
        monitor_thread.join(1.0)
    wall_time = time.monotonic() - wall_start
    transfer_time = time.monotonic() - transfer_start if payloads else 0.0
    throughput = verified_bytes / 1048576 / transfer_time if transfer_time > 0 else 0.0
    stats = percentiles(latencies)
    rows = [
        ("target", f"{options.host}:{options.tcp_port}"),
        ("chat attempted", str(chat_ok + chat_failed)),
        ("chat acked", str(chat_ok)),
        ("chat failed", str(chat_failed)),
        ("chat latency p50 ms", f"{stats.get(50, 0.0):.2f}"),
        ("chat latency p95 ms", f"{stats.get(95, 0.0):.2f}"),
        ("chat latency max ms", f"{stats.get(100, 0.0):.2f}"),
        ("transfers completed", str(transfer_ok)),
        ("transfers failed", str(transfer_failed)),
        ("transfer throughput MiB/s", f"{throughput:.2f}"),
        ("peak threads", str(peak[0])),
        ("wall seconds", f"{wall_time:.2f}"),
    ]
    if cancelled:
        rows.append(("cancelled", "partial results after Ctrl+C"))
    _print_table("M6 load results", rows)
    if cancelled:
        return 130
    if chat_failed or transfer_failed:
        print("failure summary: some attempts did not succeed; see table above")
        return 1
    return 0


def _terminal_state(events: list[tuple[str, Any]], identifier: str) -> str | None:
    """Return the latest terminal transfer state for one id, if any."""
    for kind, value in reversed(events):
        if kind == "transfer" and value["id"] == identifier \
                and value["state"] in TERMINAL_TRANSFER_STATES:
            return value["state"]
    return None


def _await_transfers(service: TransferService, events: list[tuple[str, Any]],
                     identifiers: list[str], cap: float) -> tuple[list[str], list[str]]:
    """Wait for terminal states and cancel whatever is still pending at the cap."""
    done, pending = [], list(identifiers)
    while pending and time.monotonic() < cap:
        time.sleep(0.1)
        still_pending = [i for i in pending if _terminal_state(events, i) is None]
        done.extend(i for i in pending if i not in still_pending)
        pending = still_pending
    for identifier in pending:
        service.cancel(identifier)
    return done, pending


def main(argv: list[str] | None = None) -> int:
    """Parse CLI flags and run the load plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--tcp-port", type=int, default=50001)
    parser.add_argument("--chat-count", type=int, default=100)
    parser.add_argument("--chat-concurrency", type=int, default=8)
    parser.add_argument("--transfers", type=int, default=4)
    parser.add_argument("--transfer-bytes", type=int, default=5242880)
    parser.add_argument("--duration", type=float, default=None)
    args = parser.parse_args(argv)
    if not 1 <= args.tcp_port <= 65535:
        parser.error("tcp-port must be 1-65535")
    if args.chat_concurrency < 1:
        parser.error("chat-concurrency must be at least 1")
    if args.chat_count < 0 or args.transfers < 0 or args.transfer_bytes < 0:
        parser.error("counts and sizes cannot be negative")
    if args.transfers > 0 and args.chat_count < 1:
        parser.error("transfers require chat-count of at least 1 to learn target identity")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
