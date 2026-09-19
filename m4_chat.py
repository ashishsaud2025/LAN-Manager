"""Run desktop room chat and DMs using raw UDP discovery and framed TCP."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

from PySide6.QtWidgets import QApplication

from core.chat import ChatService
from core.discovery import Hello
from core.storage import JsonLinesPostStore
from gui.main_window import MainWindow
from m1_discovery import load_identity, port_number


def main() -> int:
    """Start the desktop client and release workers after the window closes."""
    root = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) if os.name == "nt"
            else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=port_number, default=50000)
    parser.add_argument("--tcp-port", type=port_number, default=50001)
    parser.add_argument("--broadcast", default="255.255.255.255")
    parser.add_argument("--reuse-address", action="store_true")
    parser.add_argument("--identity-file", type=Path, default=root / "lan-manager/peer-id")
    parser.add_argument("--post-file", type=Path, default=root / "lan-manager/posts.jsonl")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        hello = Hello(load_identity(args.identity_file), str(uuid4()), args.name,
                      args.tcp_port, ("chat_v1", "file_v1", "posts_v1"))
        service = ChatService(hello, args.port, args.broadcast, args.reuse_address,
                              JsonLinesPostStore(args.post_file))
    except (ValueError, OSError) as error:
        logging.error("Startup failed: %s", error)
        return 1
    app = QApplication(sys.argv[:1])
    window = MainWindow(service)
    window.show()
    service.start()
    try:
        return app.exec()
    finally:
        service.stop()
        if not service.join():
            logging.error("Network workers did not finish within shutdown deadline")


if __name__ == "__main__":
    raise SystemExit(main())
