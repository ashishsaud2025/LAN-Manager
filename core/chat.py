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
from uuid import uuid4

from core.discovery import DiscoveryTransport, Hello, encode_hello
from core.diagnostics import DiagnosticsService
from core.feed import merge_page, serve_query
from core.message_journal import MessageJournal
from core.peer_repository import (
    PeerRepository, PeerRepositoryEvent, TrustState,
)
from core.post_signatures import sign_post
from core.protocol import (
    ProtocolError, envelope, recv_message, send_message, validate_envelope,
)
from core.roster import Peer, PeerRoster
from core.secure_transport import (
    PairingCandidate, SecureTransport, SecureTransportError,
)
from core.storage import PostStore
from core.transfer import TransferService

MAX_SYNC_PAGES = 20


class ChatService:
    """Run bounded chat workers and publish events to a UI-owned queue."""

    def __init__(self, hello: Hello, discovery_port: int = 50000,
                 broadcast: str = "255.255.255.255",
                 reuse_address: bool = False,
                 post_store: PostStore | None = None,
                 message_journal: MessageJournal | None = None,
                 secure_transport: SecureTransport | None = None) -> None:
        self.hello = hello
        encode_hello(hello)
        if (secure_transport is not None
                and secure_transport.local_hello != hello):
            raise ValueError("secure transport does not correspond to HELLO")
        self.discovery_options = (hello.session_id, discovery_port,
                                  broadcast, reuse_address)
        self.events: Queue[tuple[str, Any]] = Queue(maxsize=512)
        self._incoming: Queue[socket.socket] = Queue(maxsize=16)
        self._secure_incoming: Queue[socket.socket] = Queue(maxsize=16)
        self._outgoing: Queue[tuple[Peer, dict[str, Any]]] = Queue(maxsize=128)
        self._pairing: Queue[Peer] = Queue(maxsize=8)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._repository_event_lock = threading.Lock()
        self._pending_repository_event: PeerRepositoryEvent | None = None
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.secure_transport = secure_transport
        if self.secure_transport is not None:
            self.secure_transport.observe_socket = self._track
        self.peer_repository = PeerRepository(
            trust_resolver=self._resolve_trust)
        self.post_store = post_store
        self.message_journal = message_journal or MessageJournal()
        if self.secure_transport is not None:
            self.secure_transport.emit = self._event
        self._sync_cursors: dict[str, dict[str, Any] | None] = {}
        self.transfers = TransferService(
            hello, self._event, self._connect_peer, self._track)
        self.diagnostics = DiagnosticsService(
            self._event, hello.peer_id, hello.session_id)

    def start(self) -> None:
        """Start a single service lifecycle; networking initialization is asynchronous."""
        if self._threads:
            raise RuntimeError("service already started")
        self.diagnostics.start()
        targets = [self._presence, self._listen, self._receive_worker,
                   self._receive_worker, self._send_worker, self._send_worker]
        if self.secure_transport is not None:
            targets.extend((self._listen_secure, self._secure_receive_worker,
                            self._secure_receive_worker, self._pair_worker))
        for target in targets:
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

    def flush_repository_events(self) -> None:
        """Retry the latest repository revision after UI queue saturation."""
        with self._repository_event_lock:
            pending = self._pending_repository_event
        if pending is None or not self._event("peer_repository", pending):
            return
        with self._repository_event_lock:
            if self._pending_repository_event is pending:
                self._pending_repository_event = None

    def _queue_repository_event(self, event: PeerRepositoryEvent) -> None:
        with self._repository_event_lock:
            pending = self._pending_repository_event
            if pending is None or event.revision > pending.revision:
                self._pending_repository_event = event
        self.flush_repository_events()

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
        self.message_journal.record_outgoing(message, self.hello, recipients)
        for peer in recipients:
            try:
                self._outgoing.put_nowait((peer, message))
            except Full:
                self.message_journal.update_delivery(
                    message["message_id"], peer.hello.session_id, "failed",
                    "outbound queue full")
                self._event("status", f"Failed {message['message_id']} to "
                            f"{peer.hello.name}: outbound queue full")
        return message["message_id"]

    def publish_post(self, text: str, refs: list[dict[str, Any]] | None = None) -> str:
        """Persist an immutable local post and notify the UI."""
        if self.post_store is None:
            raise RuntimeError("post store is not configured")
        post = {"post_id": str(uuid4()), "author_id": self.hello.peer_id,
                "text": text, "created_ms": time.time_ns() // 1_000_000,
                "refs": refs or []}
        if self.secure_transport is not None:
            post = sign_post(post, self.secure_transport.identity)
        if not self.post_store.add(post):
            raise RuntimeError("generated duplicate post ID")
        self._event("post_published", {"post_id": post["post_id"]})
        return post["post_id"]

    def sync_posts(self, peer: Peer) -> str:
        """Queue a bounded page sync from one peer without blocking the UI."""
        if self.post_store is None:
            raise RuntimeError("post store is not configured")
        if "posts_v1" not in peer.hello.capabilities:
            raise ValueError("peer does not advertise posts_v1")
        message = envelope("POST_QUERY", self.hello.peer_id, self.hello.session_id,
                           {"cursor": self._sync_cursors.get(peer.hello.session_id),
                            "limit": 50, "author_id": None})
        try:
            self._outgoing.put_nowait((peer, message))
        except Full as error:
            raise RuntimeError("outbound queue full") from error
        return message["message_id"]

    def request_pair(self, peer: Peer) -> bool:
        """Queue one pairing request without blocking the caller."""
        if self.secure_transport is None:
            raise RuntimeError("secure transport is not configured")
        if self._stop.is_set():
            raise RuntimeError("service is stopping")
        try:
            self._pairing.put_nowait(peer)
            return True
        except Full:
            self._event("status", "Pairing request queue full")
            return False

    def accept_pair(self, request_id: str) -> PairingCandidate:
        """Accept one pending inbound pairing request."""
        if self.secure_transport is None:
            raise RuntimeError("secure transport is not configured")
        candidate = self.secure_transport.accept_pair(request_id)
        self._publish_trust_refresh()
        return candidate

    def decline_pair(self, request_id: str) -> PairingCandidate:
        """Decline one pending inbound pairing request."""
        if self.secure_transport is None:
            raise RuntimeError("secure transport is not configured")
        return self.secure_transport.decline_pair(request_id)

    def accept_outbound_pair(self, request_id: str) -> PairingCandidate:
        """Approve and pin one certificate requested by this device."""
        if self.secure_transport is None:
            raise RuntimeError("secure transport is not configured")
        candidate = self.secure_transport.accept_outbound_pair(request_id)
        self._publish_trust_refresh()
        return candidate

    def decline_outbound_pair(self, request_id: str) -> PairingCandidate:
        """Discard one outbound pairing candidate without pinning it."""
        if self.secure_transport is None:
            raise RuntimeError("secure transport is not configured")
        return self.secure_transport.decline_outbound_pair(request_id)

    def pending_pairs(self) -> tuple[PairingCandidate, ...]:
        """Return unexpired inbound pairing requests."""
        if self.secure_transport is None:
            return ()
        return self.secure_transport.pending()

    def forget_pair(self, peer_id: str) -> bool:
        """Forget one pinned peer certificate and refresh visible trust evidence."""
        if self.secure_transport is None:
            raise RuntimeError("secure transport is not configured")
        forgotten = self.secure_transport.trust_store.forget(peer_id)
        if forgotten:
            self._publish_trust_refresh()
        return forgotten

    def stop(self) -> None:
        """Request cancellation and interrupt established sockets without blocking UI."""
        self._stop.set()
        self.transfers.stop()
        self.diagnostics.stop()
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
        diagnostics_done = self.diagnostics.join(max(0, deadline - time.monotonic()))
        return (transfers_done and diagnostics_done
                and not any(thread.is_alive() for thread in self._threads))

    def _presence(self) -> None:
        transport = None
        roster = PeerRoster(self.hello.session_id)
        try:
            transport = DiscoveryTransport(*self.discovery_options)
            due = time.monotonic()
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
                event = self.peer_repository.reconcile_presence(peers, now)
                if event is not None:
                    self._queue_repository_event(event)
                self.flush_repository_events()
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

    def _listen_secure(self) -> None:
        secure_port = self.hello.secure_port
        if secure_port is None:
            self._event("status", "Secure TCP listener stopped: secure port missing")
            self.stop()
            return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                option = (socket.SO_EXCLUSIVEADDRUSE
                          if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else socket.SO_REUSEADDR)
                listener.setsockopt(socket.SOL_SOCKET, option, 1)
                listener.bind(("0.0.0.0", secure_port))
                listener.listen(16)
                listener.settimeout(0.2)
                while not self._stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except TimeoutError:
                        continue
                    self._track(conn, True)
                    try:
                        self._secure_incoming.put_nowait(conn)
                    except Full:
                        self._track(conn, False)
                        conn.close()
                        logging.warning("Secure inbound connection limit reached")
        except OSError as error:
            self._event("status", f"Secure TCP listener stopped: {error}")
            self.stop()
        finally:
            while True:
                try:
                    conn = self._secure_incoming.get_nowait()
                except Empty:
                    break
                self._track(conn, False)
                conn.close()

    def _receive_worker(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._incoming.get(timeout=0.2)
            except Empty:
                continue
            self._track(conn, True)
            try:
                with conn:
                    self._dispatch_inbound(conn, False)
            except (OSError, ValueError) as error:
                self._event("status", f"Inbound connection ended: {error}")
            finally:
                self._track(conn, False)

    def _secure_receive_worker(self) -> None:
        transport = self.secure_transport
        if transport is None:
            return
        while not self._stop.is_set():
            try:
                raw = self._secure_incoming.get(timeout=0.2)
            except Empty:
                continue
            conn = None
            transferred = False
            try:
                try:
                    channel = transport.accept(raw)
                except (OSError, SecureTransportError, ValueError) as error:
                    self._event("status", f"Secure handshake failed: {error}")
                    continue
                finally:
                    self._track(raw, False)
                if channel is None:
                    continue
                conn = channel.socket
                self._track(conn, True)
                transferred = self._dispatch_inbound(
                    conn, True, channel.peer_id, channel.session_id)
            except (OSError, ValueError) as error:
                self._event("status", f"Secure inbound connection ended: {error}")
            finally:
                self._track(raw, False)
                if conn is None:
                    try:
                        raw.close()
                    except OSError as error:
                        logging.debug("Secure raw close raced worker: %s", error)
                else:
                    if not transferred:
                        self._track(conn, False)
                        try:
                            conn.close()
                        except OSError as error:
                            logging.debug("Secure TLS close raced worker: %s", error)

    def _dispatch_inbound(self, conn: socket.socket, authenticated: bool,
                          channel_peer_id: str | None = None,
                          channel_session_id: str | None = None) -> bool:
        message = recv_message(conn)
        if message is None:
            return False
        validate_envelope(message)
        if authenticated:
            if (message["peer_id"] != channel_peer_id
                    or message["session_id"] != channel_session_id):
                raise ProtocolError("secure envelope identity mismatch")
        elif (self.secure_transport is not None
              and self.secure_transport.trust_store.get(message["peer_id"])
              is not None):
            raise ProtocolError("plaintext downgrade rejected for paired peer")
        if message["type"] == "FILE_OFFER":
            if authenticated:
                self.transfers.receive(conn, message, authenticated=True)
                return True
            dedicated = conn.dup()
            try:
                self.transfers.receive(dedicated, message)
            except (OSError, ProtocolError):
                dedicated.close()
                raise
            return False
        if message["type"] == "POST_QUERY":
            if self.post_store is None:
                raise ProtocolError("post store is not configured")
            body = serve_query(self.post_store, message["body"])
            send_message(conn, envelope("POST_PAGE", self.hello.peer_id,
                                        self.hello.session_id, body,
                                        message["message_id"]))
            return False
        if message["type"] != "CHAT":
            raise ProtocolError("listener does not accept this message type")
        body = message["body"]
        if body["scope"] == "dm" and body["to_session"] != self.hello.session_id:
            raise ProtocolError("DM addressed to a different session")
        key = (message["session_id"], message["message_id"])
        with self._lock:
            duplicate = key in self._seen
            if not duplicate:
                self._seen[key] = None
                if len(self._seen) > 1024:
                    self._seen.popitem(last=False)
        if not duplicate:
            record = self.peer_repository.get(message["session_id"])
            sender_name = (
                record.hello.name if record is not None
                and record.hello.peer_id == message["peer_id"]
                else f"Peer {message['peer_id'][:8]}")
            self.message_journal.record_incoming(
                message, sender_name, self.hello, authenticated)
        send_message(conn, envelope("ACK", self.hello.peer_id,
                                    self.hello.session_id,
                                    {"status": "accepted"},
                                    message["message_id"]))
        return False

    def _track(self, conn: socket.socket, add: bool) -> None:
        close_now = False
        with self._lock:
            if add:
                if self._stop.is_set():
                    close_now = True
                else:
                    self._active.add(conn)
            else:
                self._active.discard(conn)
        if close_now:
            try:
                conn.close()
            except OSError as error:
                logging.debug("Close raced stopped service: %s", error)

    def _connect_peer(self, peer: Peer) -> tuple[socket.socket, bool]:
        if (self.secure_transport is not None
                and self.secure_transport.trust_store.get(peer.hello.peer_id)
                is not None):
            channel = self.secure_transport.connect(peer)
            return channel.socket, True
        return (socket.create_connection(
            (peer.ip, peer.hello.tcp_port), timeout=3), False)

    def _resolve_trust(self, hello: Hello) -> TrustState:
        transport = self.secure_transport
        if transport is None:
            return TrustState.UNVERIFIED
        record = transport.trust_store.get(hello.peer_id)
        if record is None:
            return TrustState.UNVERIFIED
        if (hello.certificate_sha256 is not None
                and hello.certificate_sha256 != record.fingerprint):
            return TrustState.KEY_CHANGED
        return TrustState.PAIRED

    def _publish_trust_refresh(self) -> None:
        event = self.peer_repository.refresh_trust()
        if event is not None:
            self._queue_repository_event(event)

    def _pair_worker(self) -> None:
        transport = self.secure_transport
        if transport is None:
            return
        while not self._stop.is_set():
            try:
                peer = self._pairing.get(timeout=0.2)
            except Empty:
                continue
            try:
                candidate = transport.request_pair(peer)
                self._event("pair_outbound", candidate)
            except (OSError, SecureTransportError, ValueError) as error:
                self._event("status", f"Pairing request failed: {error}")

    def _send_worker(self) -> None:
        while not self._stop.is_set():
            try:
                peer, message = self._outgoing.get(timeout=0.2)
            except Empty:
                continue
            conn = None
            authenticated = False
            transmission_started = False
            try:
                if message["type"] == "POST_QUERY":
                    self._sync_peer(peer, message)
                    continue
                conn, authenticated = self._connect_peer(peer)
                self._track(conn, True)
                with conn:
                    transmission_started = True
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
                self.message_journal.update_delivery(
                    message["message_id"], peer.hello.session_id, "accepted",
                    "accepted by receiving application", authenticated)
                self._event("status", f"Accepted {message['message_id']} by {peer.hello.name}")
            except (OSError, SecureTransportError, ValueError) as error:
                state = "uncertain" if transmission_started else "failed"
                self.message_journal.update_delivery(
                    message["message_id"], peer.hello.session_id, state,
                    str(error))
                self._event("status", f"Failed {message['message_id']} to "
                            f"{peer.hello.name}: {error}")
            finally:
                if conn is not None:
                    conn.close()
                    self._track(conn, False)

    def _sync_peer(self, peer: Peer, query: dict[str, Any]) -> None:
        """Fetch bounded pages, retaining a continuation cursor if capped."""
        if self.post_store is None:
            raise RuntimeError("post store is not configured")
        added = duplicates = 0
        current = query
        for _ in range(MAX_SYNC_PAGES):
            conn, _ = self._connect_peer(peer)
            self._track(conn, True)
            try:
                with conn:
                    send_message(conn, current)
                    reply = recv_message(conn)
            finally:
                self._track(conn, False)
            if reply is None:
                raise ProtocolError("no post page response")
            validate_envelope(reply)
            if (reply["type"] != "POST_PAGE"
                    or reply["reply_to"] != current["message_id"]
                    or reply["peer_id"] != peer.hello.peer_id
                    or reply["session_id"] != peer.hello.session_id):
                raise ProtocolError("invalid post page response")
            page_added, page_duplicates = merge_page(self.post_store,
                                                      reply["body"]["posts"])
            added += page_added
            duplicates += page_duplicates
            cursor = reply["body"]["next_cursor"]
            if reply["body"]["complete"]:
                self._sync_cursors.pop(peer.hello.session_id, None)
                self._event("feed_updated", {"added": added,
                                              "duplicates": duplicates})
                return
            self._sync_cursors[peer.hello.session_id] = cursor
            current = envelope("POST_QUERY", self.hello.peer_id,
                               self.hello.session_id,
                               {"cursor": cursor, "limit": 50,
                                "author_id": None})
        self._event("feed_updated", {"added": added, "duplicates": duplicates,
                                     "partial": True})
