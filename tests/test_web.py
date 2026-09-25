"""Tests for web_fetch and web_search, against a local HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from timu import Context
from timu.tools import web
from timu.tools.web import (
    WebError,
    check_url,
    html_to_text,
    web_fetch_tool,
    web_search_tool,
)

FETCH = web_fetch_tool(allow_private=True)  # the test server is on 127.0.0.1


@dataclass
class Site:
    url: str = ""
    routes: dict[str, tuple[int, dict[str, str], bytes]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def site() -> Iterator[Site]:
    s = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            s.requests.append(
                {
                    "path": parts.path,
                    "query": parts.query,
                    "headers": dict(self.headers),
                }
            )
            status, headers, body = s.routes.get(parts.path, (404, {}, b"not found"))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    s.url = f"http://127.0.0.1:{httpd.server_port}"
    yield s
    httpd.shutdown()
    httpd.server_close()


def ctx(tmp_path: Path) -> Context:
    return Context(tmp_path, threading.Event())


def html(body: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": "text/html; charset=utf-8"}, body.encode()


# ---- html_to_text ----


def test_html_to_text() -> None:
    page = """<html><head><title>Docs</title><style>p{}</style>
    <script>alert("x")</script></head><body>
    <h1>Install</h1><p>Run <code>pip&nbsp;install x</code> &amp; enjoy.</p>
    <ul><li>one</li><li><a href="/two">two</a></li><li><a href="javascript:x()">three</a></li></ul>
    <noscript>enable js</noscript></body></html>"""
    assert html_to_text(page, "https://example.com/docs/") == (
        "Docs\n\nInstall\n\nRun pip install x & enjoy.\n\none\n\n"
        "two <https://example.com/two>\n\nthree"
    )


# ---- check_url ----


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "only http and https"),
        ("ftp://example.com/x", "only http and https"),
        ("http://", "no host"),
        ("http://127.0.0.1/", "non-public"),
        ("http://localhost:8080/v1", "non-public"),
        ("http://10.0.0.5/", "non-public"),
        ("http://169.254.169.254/latest/meta-data/", "non-public"),
        ("http://[::1]/", "non-public"),
    ],
)
def test_check_url_refusals(url: str, message: str) -> None:
    with pytest.raises(WebError, match=message):
        check_url(url)


def test_check_url_public_literal() -> None:
    check_url("https://93.184.215.14/")  # a public address, no DNS needed


def test_redirect_to_private_is_refused() -> None:
    handler = web._Redirects(allow_private=False)
    req = urllib.request.Request("https://example.com/")
    with pytest.raises(urllib.error.HTTPError, match="redirect refused"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")  # type: ignore[arg-type]


# ---- web_fetch ----


def test_fetch_html(site: Site, tmp_path: Path) -> None:
    site.routes["/p"] = html("<p>Hello <a href='/next'>next</a></p>")
    out = FETCH.run(ctx(tmp_path), {"url": f"{site.url}/p"})
    assert (out.is_error, out.text) == (False, f"Hello next <{site.url}/next>")
    assert site.requests[0]["headers"]["User-Agent"] == web.USER_AGENT


def test_fetch_text_json_and_charset(site: Site, tmp_path: Path) -> None:
    site.routes["/t"] = (
        200,
        {"content-type": "text/plain; charset=latin-1"},
        "caf\xe9".encode("latin-1"),
    )
    site.routes["/j"] = (200, {"content-type": "application/json"}, b'{"a": 1}')
    assert FETCH.run(ctx(tmp_path), {"url": f"{site.url}/t"}).text == "caf\xe9"
    assert FETCH.run(ctx(tmp_path), {"url": f"{site.url}/j"}).text == '{"a": 1}'


def test_fetch_follows_redirects(site: Site, tmp_path: Path) -> None:
    site.routes["/old"] = (301, {"location": "/new"}, b"")
    site.routes["/new"] = html("<p>moved</p>")
    assert FETCH.run(ctx(tmp_path), {"url": f"{site.url}/old"}).text == "moved"


def test_fetch_refuses_redirect_to_file(site: Site, tmp_path: Path) -> None:
    site.routes["/r"] = (302, {"location": "file:///etc/passwd"}, b"")
    out = FETCH.run(ctx(tmp_path), {"url": f"{site.url}/r"})
    assert out.is_error


@pytest.mark.parametrize(
    ("route", "message"),
    [
        ((404, {}, b"nope"), "http 404"),
        (
            (200, {"content-type": "image/png"}, b"\x89PNG"),
            "unsupported content type: image/png",
        ),
    ],
)
def test_fetch_errors(site: Site, tmp_path: Path, route: Any, message: str) -> None:
    site.routes["/x"] = route
    out = FETCH.run(ctx(tmp_path), {"url": f"{site.url}/x"})
    assert out.is_error
    assert message in out.text


def test_fetch_size_cap(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "MAX_FETCH", 10)
    site.routes["/big"] = (200, {"content-type": "text/plain"}, b"x" * 100)
    assert (
        FETCH.run(ctx(tmp_path), {"url": f"{site.url}/big"}).text
        == "x" * 10 + "\n[truncated at 0 KB]"
    )


def test_default_fetch_refuses_local(site: Site, tmp_path: Path) -> None:
    site.routes["/p"] = html("<p>internal</p>")
    out = web.WEB_FETCH.run(ctx(tmp_path), {"url": f"{site.url}/p"})
    assert out.is_error
    assert "non-public" in out.text
    assert site.requests == []


def test_fetch_bad_argument(tmp_path: Path) -> None:
    assert FETCH.run(ctx(tmp_path), {"url": 3}).is_error


# ---- web_search ----


def test_search(site: Site, tmp_path: Path) -> None:
    results = {
        "web": {
            "results": [
                {
                    "title": "Python <strong>docs</strong>",
                    "url": "https://docs.python.org/",
                    "description": "The <em>official</em> docs",
                },
                {"title": "no url"},
                "junk",
            ]
        }
    }
    site.routes["/search"] = (
        200,
        {"content-type": "application/json"},
        json.dumps(results).encode(),
    )
    tool = web_search_tool("key-1", f"{site.url}/search", allow_private=True)
    out = tool.run(ctx(tmp_path), {"query": "python docs", "count": 3})
    assert (
        out.text == "1. Python docs\n   https://docs.python.org/\n   The official docs"
    )
    req = site.requests[0]
    assert urllib.parse.parse_qs(req["query"]) == {"q": ["python docs"], "count": ["3"]}
    assert req["headers"]["X-Subscription-Token"] == "key-1"


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (
            (200, {"content-type": "application/json"}, b'{"web": {"results": []}}'),
            "no results",
        ),
        (
            (200, {"content-type": "application/json"}, b"not json"),
            "search failed: the response is not JSON",
        ),
        ((401, {}, b"bad key"), "search failed: http 401"),
    ],
)
def test_search_failures(site: Site, tmp_path: Path, route: Any, expected: str) -> None:
    site.routes["/search"] = route
    tool = web_search_tool("k", f"{site.url}/search", allow_private=True)
    assert tool.run(ctx(tmp_path), {"query": "q"}).text.startswith(expected)
