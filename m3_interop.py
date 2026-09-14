"""Raw TCP interoperability endpoint for desktop and future Android clients."""

from __future__ import annotations

import argparse
import logging
import socket
from uuid import uuid4

from core.protocol import (
    ProtocolError, envelope, recv_message, send_message, validate_envelope,
)
from m1_discovery import port_number


def serve(host: str, port: int) -> None:
    """Serve repeated framed ECHO requests on each accepted connection."""
    identity, session = str(uuid4()), str(uuid4())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        option = (socket.SO_EXCLUSIVEADDRUSE if hasattr(socket, "SO_EXCLUSIVEADDRUSE")
                  else socket.SO_REUSEADDR)
        listener.setsockopt(socket.SOL_SOCKET, option, 1)
        listener.bind((host, port))
        listener.listen(8)
        listener.settimeout(0.25)
        print(f"Interop listener {host}:{port}", flush=True)
        while True:
            try:
                conn, address = listener.accept()
            except TimeoutError:
                continue
            with conn:
                try:
                    while (message := recv_message(conn)) is not None:
                        validate_envelope(message)
                        if message["type"] != "ECHO":
                            raise ProtocolError("interop endpoint accepts ECHO only")
                        send_message(conn, envelope("ECHO_REPLY", identity, session,
                                                    message["body"], message["message_id"]))
                except (OSError, ProtocolError) as error:
                    logging.warning("Connection %s ended: %s", address, error)


def probe(host: str, port: int) -> None:
    """Exercise Unicode and large/small frames back to back on one connection."""
    identity, session = str(uuid4()), str(uuid4())
    with socket.create_connection((host, port), timeout=5) as conn:
        for text in ("hello", "नमस्ते", "x" * 100000, "end"):
            request = envelope("ECHO", identity, session, {"text": text})
            send_message(conn, request)
            response = recv_message(conn)
            if response is None:
                raise ProtocolError("unexpected EOF")
            validate_envelope(response)
            if (response["type"] != "ECHO_REPLY"
                    or response["reply_to"] != request["message_id"]
                    or response["body"] != request["body"]):
                raise ProtocolError("echo response mismatch")
            print(f"Verified {len(text)} characters", flush=True)


def main() -> int:
    """Run either the LAN listener or a probe to a specified host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "probe"))
    parser.add_argument("--host", help="required for probe; serve defaults to all interfaces")
    parser.add_argument("--port", type=port_number, default=50002)
    args = parser.parse_args()
    if args.mode == "probe" and not args.host:
        parser.error("probe requires --host")
    try:
        if args.mode == "serve":
            serve(args.host or "0.0.0.0", args.port)
        else:
            probe(args.host, args.port)
    except KeyboardInterrupt:
        return 0
    except (OSError, ProtocolError) as error:
        logging.error("Interop failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
