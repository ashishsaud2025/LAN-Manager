"""Bounded thread-based chat service without Qt dependencies."""

from __future__ import annotations

from collections import OrderedDict
import logging
from queue import Empty, Full, Queue
import select
import socket
import threading
import time
from typing import Any

from core.discovery import DiscoveryTransport, Hello, encode_hello
from core.protocol import (
    ProtocolError, envelope, recv_message, send_message, validate_envelope,
)
from core.roster import Peer, PeerRoster
from core.transfer import TransferService


class ChatService:
    """Run bounded chat workers and publish events to a UI-owned queue."""

    def __init__(self, hello: Hello, discovery_port: int = 50000,
                 broadcast: str = "255.255.255.255",
                 reuse_address: bool = False) -> None:
        self.hello = hello
        encode_hello(hello)
        self.discovery_options = (hello.session_id, discovery_port,
                                  broadcast, reuse_address)
        self.events: Queue[tuple[str, Any]] = Queue(maxsize=512)
        self._incoming: Queue[socket.socket] = Queue(maxsize=16)
        self._outgoing: Queue[tuple[Peer, dict[str, Any]]] = Queue(maxsize=128)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.transfers = TransferService(hello, self._event)

    def start(self) -> None:
        """Start a single service lifecycle; networking initialization is asynchronous."""
        if self._threads:
            raise RuntimeError("service already started")
        for target in (self._presence, self._listen, self._receive_worker,
                       self._receive_worker, self._send_worker, self._send_worker):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()

    def _event(self, kind: str, data: Any) -> bool:
        try:
            self.events.put_nowait((kind, data))
            return True
        except Full:
            logging.warning("Chat UI queue full; %s event not delivered", kind)
            return False

    def send(self, text: str, recipients: tuple[Peer, ...],
             direct: bool = False) -> str:
        """Queue bounded per-peer sends and return the request ID."""
        if self._stop.is_set():
            raise RuntimeError("service is stopping")
        if not recipients or (direct and len(recipients) != 1):
            raise ValueError("select one DM recipient or discover room peers")
        body = {"scope": "dm" if direct else "room", "text": text}
        if direct:
            body["to_session"] = recipients[0].hello.session_id
        message = envelope("CHAT", self.hello.peer_id, self.hello.session_id, body)
        for peer in recipients:
            try:
                self._outgoing.put_nowait((peer, message))
            except Full:
                self._event("status", f"Failed {message['message_id']} to "
                            f"{peer.hello.name}: outbound queue full")
        return message["message_id"]

    def stop(self) -> None:
        """Request cancellation and interrupt established sockets without blocking UI."""
        self._stop.set()
        self.transfers.stop()
        with self._lock:
            conns = tuple(self._active)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                logging.debug("Shutdown raced connection close: %s", error)
            try:
                conn.close()
            except OSError as error:
                logging.debug("Close raced worker: %s", error)

    def join(self, timeout: float = 6.0) -> bool:
        """Wait outside the UI event loop for workers to release resources."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        transfers_done = self.transfers.join(max(0, deadline - time.monotonic()))
        return transfers_done and not any(thread.is_alive() for thread in self._threads)

    def _presence(self) -> None:
        transport = None
        roster = PeerRoster(self.hello.session_id)
        try:
            transport = DiscoveryTransport(*self.discovery_options)
            due = time.monotonic()
            published = ()
            while not self._stop.is_set():
                now = time.monotonic()
                roster.expire(now)
                if now >= due:
                    try:
                        transport.announce(self.hello)
                    except OSError as error:
                        self._event("status", f"Discovery send failed: {error}")
                    due = now + 2
                ready, _, _ = select.select([transport.receiver], [], [], 0.2)
                if ready:
                    result = transport.receive()
                    if result is not None:
                        hello, address = result
                        roster.update(hello, address[0], time.monotonic())
                peers = roster.snapshot()
                visible = tuple((peer.hello, peer.ip) for peer in peers)
                if visible != published and self._event("roster", peers):
                    published = visible
        except OSError as error:
            self._event("status", f"Discovery stopped: {error}")
        finally:
            if transport is not None:
                transport.close()

    def _listen(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                option = (socket.SO_EXCLUSIVEADDRUSE
                          if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else socket.SO_REUSEADDR)
                listener.setsockopt(socket.SOL_SOCKET, option, 1)
                listener.bind(("0.0.0.0", self.hello.tcp_port))
                listener.listen(16)
                listener.settimeout(0.2)
                while not self._stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except TimeoutError:
                        continue
                    try:
                        self._incoming.put_nowait(conn)
                    except Full:
                        conn.close()
                        logging.warning("Inbound connection limit reached")
        except OSError as error:
            self._event("status", f"TCP listener stopped: {error}")
            self.stop()
        finally:
            while True:
                try:
                    self._incoming.get_nowait().close()
                except Empty:
                    break

    def _receive_worker(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._incoming.get(timeout=0.2)
            except Empty:
                continue
            self._track(conn, True)
            try:
                with conn:
                    message = recv_message(conn)
                    if message is None:
                        continue
                    validate_envelope(message)
                    if message["type"] == "FILE_OFFER":
                        dedicated = conn.dup()
                        try:
                            self.transfers.receive(dedicated, message)
                        except (OSError, ProtocolError):
                            dedicated.close()
                            raise
                        continue
                    if message["type"] != "CHAT":
                        raise ProtocolError("chat listener accepts CHAT only")
                    body = message["body"]
                    if body["scope"] == "dm" and body["to_session"] != self.hello.session_id:
                        raise ProtocolError("DM addressed to a different session")
                    key = (message["session_id"], message["message_id"])
                    with self._lock:
                        if key not in self._seen:
                            if not self._event("message", message):
                                raise ProtocolError("receiver UI queue full")
                            self._seen[key] = None
                            if len(self._seen) > 1024:
                                self._seen.popitem(last=False)
                    send_message(conn, envelope("ACK", self.hello.peer_id,
                                                self.hello.session_id,
                                                {"status": "accepted"},
                                                message["message_id"]))
            except (OSError, ProtocolError) as error:
                self._event("status", f"Inbound chat ended: {error}")
            finally:
                self._track(conn, False)

    def _track(self, conn: socket.socket, add: bool) -> None:
        with self._lock:
            if add:
                self._active.add(conn)
            else:
                self._active.discard(conn)

    def _send_worker(self) -> None:
        while not self._stop.is_set():
            try:
                peer, message = self._outgoing.get(timeout=0.2)
            except Empty:
                continue
            conn = None
            try:
                conn = socket.create_connection((peer.ip, peer.hello.tcp_port), timeout=3)
                self._track(conn, True)
                with conn:
                    send_message(conn, message)
                    reply = recv_message(conn)
                    if reply is None:
                        raise ProtocolError("no acknowledgement")
                    validate_envelope(reply)
                    if (reply["type"] != "ACK" or reply["reply_to"] != message["message_id"]
                            or reply["session_id"] != peer.hello.session_id
                            or reply["peer_id"] != peer.hello.peer_id
                            or reply["body"].get("status") != "accepted"):
                        raise ProtocolError("invalid acknowledgement")
                self._event("status", f"Accepted {message['message_id']} by {peer.hello.name}")
            except (OSError, ProtocolError) as error:
                self._event("status", f"Failed {message['message_id']} to "
                            f"{peer.hello.name}: {error}")
            finally:
                if conn is not None:
                    conn.close()
                    self._track(conn, False)
