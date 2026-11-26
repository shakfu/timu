"""A local HTTP server that replays scripted responses, for provider tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SSE = {"content-type": "text/event-stream"}
JSON = {"content-type": "application/json"}


@dataclass
class Resp:
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: dict(SSE))
    chunks: list[bytes] = field(default_factory=list)


@dataclass
class Server:
    url: str = ""
    responses: list[Resp] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def serve() -> Iterator[Server]:
    srv = Server()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            srv.requests.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )
            r = (
                srv.responses.pop(0)
                if srv.responses
                else Resp(599, dict(JSON), [b"unscripted"])
            )
            self.send_response(r.status)
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in r.chunks:  # separate writes, so lines can arrive split
                self.wfile.write(chunk)
                self.wfile.flush()

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    srv.url = f"http://127.0.0.1:{httpd.server_port}/v1"
    try:
        yield srv
    finally:
        httpd.shutdown()
        httpd.server_close()
