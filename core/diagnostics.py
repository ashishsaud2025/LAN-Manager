"""Bounded local diagnostics for an explicitly selected IPv4 endpoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import errno
from ipaddress import IPv4Address, ip_address
import json
import logging
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
TCP_TIMEOUT = 3.0
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


@dataclass(frozen=True)
class _Request:
    kind: str
    request_id: str
    address: str = ""
    port: int | None = None
    peer_id: str | None = None
    session_id: str | None = None


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

    def ping(self, address: str) -> str:
        """Queue one bounded system ping for a numeric IPv4 address."""
        target = _validate_address(address)
        return self._queue(_Request("ping", str(uuid4()), target))

    def tcp_connect(self, address: str, port: int) -> str:
        """Queue one TCP handshake check without sending application data."""
        target = _validate_address(address)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535")
        return self._queue(_Request("tcp", str(uuid4()), target, port))

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
        command = (["ping", "-n", "1", "-w", "1500", request.address]
                   if self._platform.startswith("win")
                   else ["ping", "-c", "1", "-W", "2", request.address])
        started = time.monotonic()
        try:
            code, output, error = self._command_runner(command, PING_TIMEOUT)
            duration = (time.monotonic() - started) * 1000
            state = "cancelled" if self._cancelled() else (
                "reachable" if code == 0 else "no_reply")
            detail = _detail(output if code == 0 else error or output)
            result = ProbeResult(request.request_id, "ping", request.address, None,
                                 state, duration, detail)
        except (DiagnosticsError, OSError) as error:
            state = "cancelled" if self._cancelled() else "failed"
            result = ProbeResult(request.request_id, "ping", request.address, None,
                                 state, None, str(error))
        self._event("diagnostic_result", result)

    def _tcp_connect(self, request: _Request) -> None:
        self._event("diagnostic_started", ProbeResult(
            request.request_id, "tcp", request.address, request.port, "running"))
        started = time.monotonic()
        active_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        active_socket.settimeout(TCP_TIMEOUT)
        with self._lock:
            self._socket = active_socket
        try:
            active_socket.connect((request.address, request.port))
            result = ProbeResult(
                request.request_id, "tcp", request.address, request.port,
                "cancelled" if self._cancelled() else "reachable",
                (time.monotonic() - started) * 1000,
                "TCP handshake completed; no application data was sent")
        except ConnectionRefusedError as error:
            result = self._tcp_failure(request, started, "refused", error)
        except (TimeoutError, socket.timeout) as error:
            result = self._tcp_failure(request, started, "timed_out", error)
        except OSError as error:
            state = ("network_unreachable" if error.errno in {
                errno.ENETUNREACH, errno.EHOSTUNREACH} else "failed")
            result = self._tcp_failure(request, started, state, error)
        finally:
            active_socket.close()
            with self._lock:
                if self._socket is active_socket:
                    self._socket = None
        self._event("diagnostic_result", result)

    def _tcp_failure(self, request: _Request, started: float, state: str,
                     error: OSError) -> ProbeResult:
        if self._cancelled():
            state = "cancelled"
        return ProbeResult(request.request_id, "tcp", request.address, request.port,
                           state, (time.monotonic() - started) * 1000, str(error))

    def _echo(self, request: _Request) -> None:
        self._event("diagnostic_started", ProbeResult(
            request.request_id, "echo", request.address, request.port, "running"))
        started = time.monotonic()
        active_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        active_socket.settimeout(TCP_TIMEOUT)
        with self._lock:
            self._socket = active_socket
        try:
            active_socket.connect((request.address, request.port))
            body = {"text": f"lan-atlas:{request.request_id}"}
            message = envelope(
                "ECHO", self._peer_id, self._session_id, body)
            send_message(active_socket, message, ECHO_TIMEOUT)
            reply = recv_message(active_socket, ECHO_TIMEOUT)
            if reply is None:
                raise ProtocolError("ECHO endpoint closed without a reply")
            validate_envelope(reply)
            if (reply["type"] != "ECHO_REPLY"
                    or reply["reply_to"] != message["message_id"]
                    or reply["body"] != body
                    or reply["peer_id"] != request.peer_id
                    or reply["session_id"] != request.session_id):
                raise ProtocolError("ECHO reply correlation or identity mismatch")
            result = ProbeResult(
                request.request_id, "echo", request.address, request.port,
                "cancelled" if self._cancelled() else "compatible",
                (time.monotonic() - started) * 1000,
                "Correlated LAN Manager ECHO_REPLY matched the advertised session")
        except ProtocolError as error:
            result = self._echo_failure(request, started, "incompatible", error)
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
                      error: Exception) -> ProbeResult:
        if self._cancelled():
            state = "cancelled"
        return ProbeResult(request.request_id, "echo", request.address, request.port,
                           state, (time.monotonic() - started) * 1000, str(error))

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


def _detail(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1][:512] if lines else ""
