"""A scoring endpoint that enforces a gap between submissions"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ScoringServer:
    def __init__(self, gap: float, scores: list[float]) -> None:
        self.gap = gap
        self.scores = list(scores)
        self.last = 0.0
        self.seen: set[str] = set()
        self.calls = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                server.calls += 1
                now = time.monotonic()
                if now - server.last < server.gap:
                    self.send_response(429)
                    self.send_header("Retry-After", f"{server.gap - (now - server.last):.2f}")
                    self.end_headers()
                    return
                server.last = now
                digest = hashlib.sha256(body).hexdigest()
                score = (
                    None
                    if digest in server.seen
                    else (server.scores.pop(0) if server.scores else 0.0)
                )
                server.seen.add(digest)
                payload = json.dumps({"result": {"similarity": score}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/submit"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> ScoringServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
