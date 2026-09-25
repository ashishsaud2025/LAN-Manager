"""Bounded local diagnostics for an explicitly selected IPv4 endpoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import errno
from ipaddress import IPv4Address, ip_address
import json
import logging
import math
from queue import Empty, Full, Queue
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from core.protocol import (
    ProtocolError, envelope, recv_message, send_message, validate_envelope,
)

MAX_NEIGHBORS = 256
MAX_COMMAND_OUTPUT = 262_144
REQUEST_QUEUE_SIZE = 16
INVENTORY_TIMEOUT = 4.0
PING_TIMEOUT = 3.0
PING_MIN_TIMEOUT = 0.5
PING_MAX_TIMEOUT = 10.0
PING_MAX_COUNT = 10
PING_MAX_PAYLOAD = 1400
PING_INTERVAL_S = 0.2
TCP_TIMEOUT = 3.0
TCP_MIN_TIMEOUT = 0.5
TCP_MAX_TIMEOUT = 10.0
TCP_MAX_ATTEMPTS = 5
ECHO_TIMEOUT = 5.0
_MAC_PATTERN = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
_WINDOWS_NEIGHBOR_STATES = {
    0: "unreachable", 1: "incomplete", 2: "probe", 3: "delay",
    4: "stale", 5: "reachable", 6: "permanent",
}


class DiagnosticsError(Exception):
    """Report an unavailable or malformed local diagnostics operation."""


@dataclass(frozen=True)
class Neighbor:
    """One bounded row read from the operating system neighbor cache."""

    address: str
    mac_address: str | None
    interface: str | None
    state: str | None


@dataclass(frozen=True)
class NeighborSnapshot:
    """One complete neighbor-cache observation or explicit refresh failure."""

    request_id: str
    entries: tuple[Neighbor, ...]
    source: str
    error: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    """Evidence from one selected ping or TCP-connect operation."""

    request_id: str
    kind: str
    address: str
    port: int | None
    state: str
    duration_ms: float | None = None
    detail: str = ""
    rtt_ms: float | None = None
    rtt_samples: tuple[float, ...] = ()
    loss_pct: float | None = None
    jitter_ms: float | None = None
    ttl: int | None = None


@dataclass(frozen=True)
class _Request:
    kind: str
    request_id: str
    address: str = ""
    port: int | None = None
    peer_id: str | None = None
    session_id: str | None = None
    count: int = 1
    timeout: float = PING_TIMEOUT
    payload_size: int = 32
    attempts: int = 1


CommandRunner = Callable[[list[str], float], tuple[int, str, str]]
EventSink = Callable[[str, Any], bool]


def parse_windows_neighbors(payload: str) -> tuple[Neighbor, ...]:
    """Parse bounded JSON produced by Windows Get-NetNeighbor."""
    try:
        raw = json.loads(payload) if payload.strip() else []
    except json.JSONDecodeError as error:
        raise DiagnosticsError("Windows neighbor output was not valid JSON") from error
    rows = raw if isinstance(raw, list) else [raw]
    if not all(isinstance(row, dict) for row in rows):
        raise DiagnosticsError("Windows neighbor output had an invalid shape")
    values = (
        _neighbor(
            row.get("IPAddress"), row.get("LinkLayerAddress"),
            row.get("InterfaceAlias"), row.get("State"))
        for row in rows[:MAX_NEIGHBORS * 2]
    )
    return _deduplicate(item for item in values if item is not None)


def parse_linux_neighbors(payload: str) -> tuple[Neighbor, ...]:
    """Parse bounded JSON produced by Linux ip -j neigh."""
    try:
        rows = json.loads(payload) if payload.strip() else []
    except json.JSONDecodeError as error:
        raise DiagnosticsError("Linux neighbor output was not valid JSON") from error
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise DiagnosticsError("Linux neighbor output had an invalid shape")
    values = (
        _neighbor(row.get("dst"), row.get("lladdr"), row.get("dev"), row.get("state"))
        for row in rows[:MAX_NEIGHBORS * 2]
    )
    return _deduplicate(item for item in values if item is not None)


def _neighbor(address: object, mac_address: object, interface: object,
              state: object) -> Neighbor | None:
    if not isinstance(address, str):
        return None
    try:
        parsed = ip_address(address)
    except ValueError:
        return None
    if not isinstance(parsed, IPv4Address) or parsed.is_multicast or parsed.is_unspecified:
        return None
    mac = _normalize_mac(mac_address)
    if parsed == IPv4Address("255.255.255.255") or mac == "ff:ff:ff:ff:ff:ff":
        return None
    interface_value = interface[:128] if isinstance(interface, str) and interface else None
    if isinstance(state, list):
        state = ", ".join(str(item) for item in state[:4])
    elif isinstance(state, int) and not isinstance(state, bool):
        state = _WINDOWS_NEIGHBOR_STATES.get(state, f"state {state}")
    state_value = state[:64] if isinstance(state, str) and state else None
    return Neighbor(str(parsed), mac, interface_value, state_value)


def _normalize_mac(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", ":")
    if normalized == "00:00:00:00:00:00":
        return None
    return normalized if _MAC_PATTERN.fullmatch(normalized) else None


def _deduplicate(values: Any) -> tuple[Neighbor, ...]:
    result: list[Neighbor] = []
    seen: set[tuple[str | None, str]] = set()
    for value in values:
        key = (value.interface, value.address)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= MAX_NEIGHBORS:
            break
    result.sort(key=lambda item: tuple(int(part) for part in item.address.split(".")))
    return tuple(result)


class DiagnosticsService:
    """Run neighbor refreshes and selected probes on one cancellable worker."""

    def __init__(self, event_sink: EventSink, peer_id: str | None = None,
                 session_id: str | None = None, platform: str | None = None,
                 command_runner: CommandRunner | None = None) -> None:
        self._event = event_sink
        self._peer_id = peer_id or str(uuid4())
        self._session_id = session_id or str(uuid4())
        self._platform = platform or sys.platform
        self._command_runner = command_runner or self._run_command
        self._requests: Queue[_Request | None] = Queue(maxsize=REQUEST_QUEUE_SIZE)
        self._stop = threading.Event()
        self._cancel_current = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        """Start the single diagnostics worker."""
        if self._thread is not None:
            raise RuntimeError("diagnostics already started")
        self._thread = threading.Thread(
            target=self._worker, name="lan-diagnostics", daemon=True)
        self._thread.start()

    def refresh_neighbors(self) -> str:
        """Queue one operating-system neighbor-cache refresh."""
        return self._queue(_Request("inventory", str(uuid4())))

    def ping(self, address: str, count: int = 1,
             timeout: float = PING_TIMEOUT, payload_size: int = 32) -> str:
        """Queue one bounded multi-packet system ping for an IPv4 address."""
        target = _validate_address(address)
        return self._queue(_Request(
            "ping", str(uuid4()), target,
            count=_validate_count(count), timeout=_validate_timeout(timeout),
            payload_size=_validate_payload(payload_size)))

    def tcp_connect(self, address: str, port: int,
                    timeout: float = TCP_TIMEOUT, attempts: int = 1) -> str:
        """Queue TCP handshake checks without sending application data."""
        target = _validate_address(address)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535")
        return self._queue(_Request(
            "tcp", str(uuid4()), target, port,
            timeout=_validate_tcp_timeout(timeout),
            attempts=_validate_attempts(attempts)))

    def echo(self, address: str, port: int, peer_id: str,
             session_id: str) -> str:
        """Queue one correlated LAN Manager ECHO exchange."""
        target = _validate_address(address)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("ECHO port must be between 1 and 65535")
        return self._queue(_Request(
            "echo", str(uuid4()), target, port,
            _validate_uuid(peer_id, "peer ID"),
            _validate_uuid(session_id, "session ID")))

    def cancel(self) -> None:
        """Cancel the operation currently running without stopping the worker."""
        self._cancel_current.set()
        with self._lock:
            process, active_socket = self._process, self._socket
        if process is not None:
            try:
                process.terminate()
            except OSError as error:
                logging.debug("Diagnostic process termination raced exit: %s", error)
        if active_socket is not None:
            try:
                active_socket.close()
            except OSError as error:
                logging.debug("Diagnostic socket close raced completion: %s", error)

    def stop(self) -> None:
        """Reject new work, cancel active work, and wake the worker."""
        self._stop.set()
        self.cancel()
        try:
            self._requests.put_nowait(None)
        except Full:
            pass

    def join(self, timeout: float = 6.0) -> bool:
        """Wait for the diagnostics worker outside the UI thread."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _queue(self, request: _Request) -> str:
        if self._stop.is_set():
            raise RuntimeError("diagnostics are stopping")
        try:
            self._requests.put_nowait(request)
        except Full as error:
            raise RuntimeError("diagnostics queue is full") from error
        return request.request_id

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                request = self._requests.get(timeout=0.2)
            except Empty:
                continue
            if request is None:
                break
            self._cancel_current.clear()
            if request.kind == "inventory":
                self._refresh(request)
            elif request.kind == "ping":
                self._ping(request)
            elif request.kind == "tcp":
                self._tcp_connect(request)
            else:
                self._echo(request)

    def _refresh(self, request: _Request) -> None:
        source = "OS neighbor cache"
        try:
            command, parser = self._inventory_adapter()
            code, output, error = self._command_runner(command, INVENTORY_TIMEOUT)
            if self._cancelled():
                raise DiagnosticsError("refresh cancelled")
            if code != 0:
                raise DiagnosticsError(_detail(error) or f"neighbor command exited {code}")
            entries = parser(output)
            snapshot = NeighborSnapshot(request.request_id, entries, source)
        except (DiagnosticsError, OSError) as error:
            snapshot = NeighborSnapshot(request.request_id, (), source, str(error))
        self._event("neighbor_snapshot", snapshot)

    def _inventory_adapter(self) -> tuple[
            list[str], Callable[[str], tuple[Neighbor, ...]]]:
        if self._platform.startswith("win"):
            script = (
                "Get-NetNeighbor -AddressFamily IPv4 | "
                "Select-Object IPAddress,LinkLayerAddress,InterfaceAlias,State | "
                "ConvertTo-Json -Compress")
            return (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                    parse_windows_neighbors)
        if self._platform.startswith("linux"):
            return ["ip", "-j", "neigh"], parse_linux_neighbors
        raise DiagnosticsError("neighbor inventory is not supported on this platform")

    def _ping(self, request: _Request) -> None:
        self._event("diagnostic_started", ProbeResult(
            request.request_id, "ping", request.address, None, "running"))
        if self._platform.startswith("win"):
            command = ["ping", "-n", str(request.count),
                       "-w", str(int(request.timeout * 1000)),
                       "-l", str(request.payload_size), request.address]
        else:
            command = ["ping", "-c", str(request.count),
                       "-W", str(max(1, math.ceil(request.timeout))),
                       "-i", str(PING_INTERVAL_S),
                       "-s", str(request.payload_size), request.address]
        started = time.perf_counter()
        try:
            code, output, error = self._command_runner(
                command, request.timeout * request.count + 5.0)
            duration = (time.perf_counter() - started) * 1000
            state = "cancelled" if self._cancelled() else (
                "reachable" if code == 0 else "no_reply")
            detail = _detail(output if code == 0 else error or output)
            samples = (parse_ping_rtts(output, request.count)
                       if state == "reachable" else ())
            replies = min(len(samples), request.count)
            loss = ((request.count - replies) / request.count * 100.0
                    if state == "reachable" else None)
            _, average, _, jitter = ping_statistics(samples)
            rtt = (average if average is not None
                   else parse_ping_rtt(output))
            result = ProbeResult(request.request_id, "ping", request.address,
                                 None, state, duration, detail, rtt, samples,
                                 loss, jitter,
                                 parse_ping_ttl(output)
                                 if state == "reachable" else None)
        except (DiagnosticsError, OSError) as error:
            state = "cancelled" if self._cancelled() else "failed"
            result = ProbeResult(request.request_id, "ping", request.address, None,
                                 state, None, str(error))
        self._event("diagnostic_result", result)

    def _tcp_connect(self, request: _Request) -> None:
        self._event("diagnostic_started", ProbeResult(
            request.request_id, "tcp", request.address, request.port, "running"))
        started = time.perf_counter()
        outcomes: list[str] = []
        handshake_ms: float | None = None
        state = "failed"
        for attempt in range(1, request.attempts + 1):
            if self._cancelled():
                state = "cancelled"
                break
            active_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            active_socket.settimeout(request.timeout)
            with self._lock:
                self._socket = active_socket
            try:
                attempt_start = time.perf_counter()
                active_socket.connect((request.address, request.port))
                handshake_ms = (time.perf_counter() - attempt_start) * 1000
                state = "cancelled" if self._cancelled() else "reachable"
                outcomes.append(f"attempt {attempt}: connected")
                active_socket.close()
                with self._lock:
                    if self._socket is active_socket:
                        self._socket = None
                break
            except ConnectionRefusedError as error:
                outcomes.append(f"attempt {attempt}: refused ({error})")
                state = "cancelled" if self._cancelled() else "refused"
            except (TimeoutError, socket.timeout) as error:
                outcomes.append(f"attempt {attempt}: timed out ({error})")
                state = "cancelled" if self._cancelled() else "timed_out"
            except OSError as error:
                outcomes.append(f"attempt {attempt}: {error}")
                if self._cancelled():
                    state = "cancelled"
                else:
                    state = ("network_unreachable" if error.errno in {
                        errno.ENETUNREACH, errno.EHOSTUNREACH} else "failed")
            finally:
                active_socket.close()
                with self._lock:
                    if self._socket is active_socket:
                        self._socket = None
        duration = (time.perf_counter() - started) * 1000
        detail = ("TCP handshake completed; no application data was sent"
                  if state == "reachable"
                  else "; ".join(outcomes)[:512] or "TCP handshake failed")
        rtt = (handshake_ms if handshake_ms is not None
               and _valid_rtt(handshake_ms) else None)
        if rtt is None and state == "refused" and _valid_rtt(duration):
            rtt = duration
        self._event("diagnostic_result", ProbeResult(
            request.request_id, "tcp", request.address, request.port,
            state, duration, detail, rtt))

    def _echo(self, request: _Request) -> None:
        self._event("diagnostic_started", ProbeResult(
            request.request_id, "echo", request.address, request.port, "running"))
        started = time.perf_counter()
        active_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        active_socket.settimeout(TCP_TIMEOUT)
        with self._lock:
            self._socket = active_socket
        exchange_ms: float | None = None
        try:
            active_socket.connect((request.address, request.port))
            body = {"text": f"lan-atlas:{request.request_id}"}
            message = envelope(
                "ECHO", self._peer_id, self._session_id, body)
            exchange_start = time.perf_counter()
            send_message(active_socket, message, ECHO_TIMEOUT)
            reply = recv_message(active_socket, ECHO_TIMEOUT)
            exchange_ms = (time.perf_counter() - exchange_start) * 1000
            if reply is None:
                raise ProtocolError("ECHO endpoint closed without a reply")
            validate_envelope(reply)
            if (reply["type"] != "ECHO_REPLY"
                    or reply["reply_to"] != message["message_id"]
                    or reply["body"] != body
                    or reply["peer_id"] != request.peer_id
                    or reply["session_id"] != request.session_id):
                raise ProtocolError("ECHO reply correlation or identity mismatch")
            duration = (time.perf_counter() - started) * 1000
            state = "cancelled" if self._cancelled() else "compatible"
            rtt = exchange_ms if state == "compatible" and _valid_rtt(exchange_ms) else None
            result = ProbeResult(
                request.request_id, "echo", request.address, request.port,
                state, duration,
                "Correlated LAN Manager ECHO_REPLY matched the advertised session",
                rtt)
        except ProtocolError as error:
            result = self._echo_failure(request, started, "incompatible", error,
                                        exchange_ms)
        except ConnectionRefusedError as error:
            result = self._echo_failure(request, started, "refused", error)
        except (TimeoutError, socket.timeout) as error:
            result = self._echo_failure(request, started, "timed_out", error)
        except OSError as error:
            state = ("network_unreachable" if error.errno in {
                errno.ENETUNREACH, errno.EHOSTUNREACH} else "failed")
            result = self._echo_failure(request, started, state, error)
        finally:
            active_socket.close()
            with self._lock:
                if self._socket is active_socket:
                    self._socket = None
        self._event("diagnostic_result", result)

    def _echo_failure(self, request: _Request, started: float, state: str,
                      error: Exception, exchange_ms: float | None = None) -> ProbeResult:
        if self._cancelled():
            state = "cancelled"
        duration = (time.perf_counter() - started) * 1000
        rtt: float | None = None
        if state == "incompatible" and exchange_ms is not None and _valid_rtt(exchange_ms):
            rtt = exchange_ms
        elif state == "refused" and _valid_rtt(duration):
            rtt = duration
        return ProbeResult(request.request_id, "echo", request.address, request.port,
                           state, duration, str(error), rtt)

    def _run_command(self, command: list[str], timeout: float) -> tuple[int, str, str]:
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", shell=False)
        except OSError as error:
            raise DiagnosticsError(f"command unavailable: {command[0]}") from error
        with self._lock:
            self._process = process
        try:
            try:
                output, error = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as timeout_error:
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise DiagnosticsError("diagnostic command timed out") from timeout_error
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
        if len(output.encode("utf-8")) + len(error.encode("utf-8")) > MAX_COMMAND_OUTPUT:
            raise DiagnosticsError("diagnostic command output exceeded the limit")
        return process.returncode, output, error

    def _cancelled(self) -> bool:
        return self._stop.is_set() or self._cancel_current.is_set()


def _validate_address(value: str) -> str:
    try:
        parsed = ip_address(value.strip())
    except ValueError as error:
        raise ValueError("enter a numeric IPv4 address") from error
    if not isinstance(parsed, IPv4Address) or parsed.is_multicast or parsed.is_unspecified:
        raise ValueError("enter a unicast IPv4 address")
    return str(parsed)


def _validate_uuid(value: str, label: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical UUID") from error
    return value


def _validate_count(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Ping count must be an integer between 1 and "
                         f"{PING_MAX_COUNT}")
    if not 1 <= value <= PING_MAX_COUNT:
        raise ValueError("Ping count must be between 1 and "
                         f"{PING_MAX_COUNT}")
    return value


def _validate_timeout(value: float) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)
            or not PING_MIN_TIMEOUT <= value <= PING_MAX_TIMEOUT):
        raise ValueError("Ping timeout must be between "
                         f"{PING_MIN_TIMEOUT} and {PING_MAX_TIMEOUT} seconds")
    return float(value)


def _validate_payload(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Ping payload must be an integer between 0 and "
                         f"{PING_MAX_PAYLOAD}")
    if not 0 <= value <= PING_MAX_PAYLOAD:
        raise ValueError("Ping payload must be between 0 and "
                         f"{PING_MAX_PAYLOAD} bytes")
    return value


def _validate_tcp_timeout(value: float) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)
            or not TCP_MIN_TIMEOUT <= value <= TCP_MAX_TIMEOUT):
        raise ValueError("TCP timeout must be between "
                         f"{TCP_MIN_TIMEOUT} and {TCP_MAX_TIMEOUT} seconds")
    return float(value)


def _validate_attempts(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("TCP attempts must be an integer between 1 and "
                         f"{TCP_MAX_ATTEMPTS}")
    if not 1 <= value <= TCP_MAX_ATTEMPTS:
        raise ValueError("TCP attempts must be between 1 and "
                         f"{TCP_MAX_ATTEMPTS}")
    return value


def parse_ping_rtt(output: str) -> float | None:
    """Parse measured ICMP RTT without inventing samples.

    Prefers the Windows average and Linux avg summary; falls back to the
    single-reply time= field used by one-packet probes. Returns None when
    output has no parseable timing or resolution is zero.
    """
    if not output:
        return None
    match = re.search(r"Average\s*=\s*<?\s*(\d+(?:\.\d+)?)\s*ms", output)
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return value if _valid_rtt(value) else None
    match = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
        r"[\d.]+/([\d.]+)/[\d.]+/[\d.]+\s*ms", output)
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return value if _valid_rtt(value) else None
    match = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", output)
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return value if _valid_rtt(value) else None
    return None


def _valid_rtt(value: float) -> bool:
    return math.isfinite(value) and 0 < value < 60000


def parse_ping_rtts(output: str, limit: int = PING_MAX_COUNT) -> tuple[float, ...]:
    """Collect per-reply RTT samples without inventing missing ones."""
    if not output or limit <= 0:
        return ()
    samples = []
    for match in re.finditer(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", output):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if _valid_rtt(value):
            samples.append(value)
        if len(samples) >= limit:
            break
    return tuple(samples)


def parse_ping_ttl(output: str) -> int | None:
    """Parse the first reported TTL without claiming packet details."""
    if not output:
        return None
    match = re.search(r"\bttl=(\d{1,3})", output, re.IGNORECASE)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 255 else None


def ping_statistics(samples: tuple[float, ...]) -> tuple[
        float | None, float | None, float | None, float | None]:
    """Return min, average, max, and jitter for measured samples only."""
    if not samples:
        return None, None, None, None
    minimum, maximum = min(samples), max(samples)
    average = sum(samples) / len(samples)
    jitter = (sum(abs(first - second)
                  for first, second in zip(samples, samples[1:]))
              / (len(samples) - 1)) if len(samples) > 1 else None
    return minimum, average, maximum, jitter


def _detail(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1][:512] if lines else ""
