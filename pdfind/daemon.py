"""Optional search daemon.

Loading the ONNX model and memory-mapping the index costs about a second. That
is fine for a shell command and far too slow for a launcher bar, so `pdfind
serve` keeps both resident and answers queries over a unix socket.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from dataclasses import asdict
from pathlib import Path

from . import env
from .config import Config, load_config
from .filters import Filter
from .hit import Hit

SOCKET_PATH = env.runtime_dir() / "pdfind.sock"

_searcher = None  # type: ignore[var-annotated]
_lock = threading.Lock()


class Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            self._send({"error": "bad request"})
            return

        action = req.get("action", "search")
        try:
            if action == "ping":
                self._send({"ok": True})
            elif action == "reload":
                with _lock:
                    assert _searcher is not None
                    _searcher.load()
                self._send({"ok": True})
            elif action == "context":
                # Full text of every chunk on one page — the preview pane
                # wants more than the 200-character result snippet.
                with _lock:
                    assert _searcher is not None
                    if _searcher._lookup is None:
                        _searcher.load()
                    path, page = req.get("path"), int(req.get("page", 0))
                    parts = [text for _, (p, pg, text)
                             in sorted(_searcher._lookup.items())
                             if p == path and pg == page]
                self._send({"text": "\n\n".join(parts)[:8000]})
            elif action == "search":
                with _lock:
                    assert _searcher is not None
                    hits = _searcher.search(
                        req.get("q", ""),
                        limit=int(req.get("limit", 10)),
                        min_score=float(req.get("min_score", 0.0)),
                        filters=Filter.from_dict(req.get("filters")),
                    )
                self._send({"hits": [asdict(h) for h in hits]})
            else:
                self._send({"error": f"unknown action {action!r}"})
        except Exception as exc:  # keep the daemon alive across bad queries
            self._send({"error": str(exc)})

    def _send(self, payload: dict) -> None:
        self.wfile.write((json.dumps(payload) + "\n").encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(cfg: Config | None = None) -> None:
    global _searcher
    from .search import Searcher  # heavy: numpy + the ONNX model

    cfg = cfg or load_config()
    _searcher = Searcher(cfg)
    _searcher.load()

    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)

    server = Server(str(SOCKET_PATH), Handler)
    os.chmod(SOCKET_PATH, 0o600)
    print(f"pdfind daemon listening on {SOCKET_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)


def request(payload: dict, timeout: float = 15.0) -> dict | None:
    """Talk to the daemon. Returns None if it is not running."""
    if not SOCKET_PATH.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(SOCKET_PATH))
            sock.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                part = sock.recv(65536)
                if not part:
                    break
                buf += part
    except (OSError, socket.timeout):
        return None
    if not buf:
        return None
    try:
        return json.loads(buf)
    except json.JSONDecodeError:
        return None


def search_via_daemon(query: str, limit: int, min_score: float = 0.0,
                      filters: Filter | None = None) -> list[Hit] | None:
    resp = request({"action": "search", "q": query, "limit": limit,
                    "min_score": min_score,
                    "filters": filters.to_dict() if filters else None})
    if not resp or "hits" not in resp:
        return None
    return [Hit(**h) for h in resp["hits"]]


def page_context(path: str, page: int) -> str | None:
    resp = request({"action": "context", "path": path, "page": page})
    return resp.get("text") if resp else None


def notify_reload() -> None:
    request({"action": "reload"}, timeout=60)
