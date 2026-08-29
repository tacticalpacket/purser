"""Serve the dashboard page on the loopback interface, and nothing else.

This page renders someone's entire financial position. Three properties below
are security properties, not preferences, and each has a reason written next to
it because the reason is what a future change has to argue with.

**Loopback only.** The listener binds `LOOPBACK` -- the literal 127.0.0.1 --
and there is no host argument anywhere in this module or in the CLI. Binding
0.0.0.0 would publish the owner's balances to every machine on the network,
including a cafe's, with no authentication of any kind in front of them. There
is deliberately nothing to override: `tests/test_dashboard.py` asserts the bound
address is loopback.

**One route.** `GET /` returns the page. Everything else is 404 with an empty
body. There is no static-file handler, no directory listing and no path
resolution against the filesystem, so there is no path-traversal surface: the
page's CSS and JS are inlined into the one response rather than served from
disk. That is also why the page works with the network cable pulled -- it has
no subresources at all.

**Untrusted text.** Merchant descriptions come from an institution and nobody
sanitised them. `render_page` puts the document inside a
`<script type="application/json">` data block, which the browser never
executes, and escapes `<`, `>` and `&` to their JSON `\\uXXXX` forms so that no
byte in the document can close that element. `app.js` reads it back with
textContent and inserts every value with textContent. A description containing
`</script><script>alert(1)</script>` therefore reaches the screen as those
literal characters.

The response also carries a `Content-Security-Policy` with `default-src 'none'`
and a fresh per-response nonce for the one `<style>` and the one `<script>`.
No remote origin is allowed to load and no inline script without the nonce can
run, so an escaping mistake fails closed rather than executing. `no-store`
keeps the page out of the browser's disk cache.
"""

from __future__ import annotations

import json
import re
import secrets
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

#: The only address this server ever binds. See the module docstring.
LOOPBACK = "127.0.0.1"

#: The only path that returns a body.
ROUTE = "/"

_ASSETS = Path(__file__).resolve().parent


def _asset(name: str) -> str:
    return (_ASSETS / name).read_text(encoding="utf-8")


def inline_safely(text: str, element: str) -> str:
    """Make `text` safe to place inside `<element>...</element>` in HTML.

    An HTML parser ends a `<script>` or `<style>` element at the first
    `</script` or `</style` in its raw text, wherever that lands -- inside a
    string literal, inside a comment, anywhere. Nothing in the JavaScript or CSS
    grammar protects it, so a source comment that merely *mentions* the closing
    tag truncates the element and the browser parses the rest of the file as
    markup.

    That is not hypothetical here: app.js's own header comment explains the
    `</script>` breakout it defends against, and writing that sentence broke the
    page. The escape is the standard one -- a backslash before the slash, which
    the HTML tokenizer no longer recognises as a closing tag while JavaScript
    and CSS read it as the same characters they always did (`"<\\/script>"` is
    `"</script>"`, and in a comment it is inert either way).
    """
    return re.sub(rf"</\s*({element})", r"<\\/\1", text, flags=re.IGNORECASE)


def escape_document(document: dict) -> str:
    """Serialize `document` so it cannot break out of a JSON data block.

    `<`, `>` and `&` become `\\u003c`, `\\u003e` and `\\u0026`. Those are legal
    JSON string escapes, so `JSON.parse` returns the original characters -- but
    the bytes sitting in the HTML can no longer form `</script`, an entity, or a
    tag of any kind. `ensure_ascii` already removes every non-ASCII byte from
    the equation.

    This is the whole defence at the serialization boundary; `app.js` carries
    the other half by never touching innerHTML.
    """
    text = json.dumps(document, ensure_ascii=True, allow_nan=False, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_page(document: dict, nonce: str | None = None) -> str:
    """The complete, self-contained HTML page for one document."""
    nonce = nonce or secrets.token_urlsafe(16)
    html = _asset("page.html")
    # Style and script first, document last: a document that happened to
    # contain a placeholder string is then inert, because nothing substitutes
    # after it. (It cannot contain one anyway once escaped, but ordering the
    # replacements this way means that does not have to be true.)
    html = html.replace("{{NONCE}}", nonce)
    html = html.replace("{{STYLE}}", inline_safely(_asset("style.css"), "style"))
    html = html.replace("{{SCRIPT}}", inline_safely(_asset("app.js"), "script"))
    html = html.replace("{{DOCUMENT_JSON}}", escape_document(document))
    return html


def _csp(nonce: str) -> str:
    return "; ".join(
        [
            "default-src 'none'",
            f"style-src 'nonce-{nonce}'",
            f"script-src 'nonce-{nonce}'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )


class DashboardHandler(BaseHTTPRequestHandler):
    """Answers `GET /` with the page. Every other path is 404."""

    server_version = "purser-dashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, document: dict, **kwargs):
        self._document = document
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
        if urlsplit(self.path).path != ROUTE:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        nonce = secrets.token_urlsafe(16)
        body = render_page(self._document, nonce).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _csp(nonce))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200 if urlsplit(self.path).path == ROUTE else 404)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        """Silence the access log.

        The default writes the request line to stderr on every hit. Nothing
        about a local page view is worth logging, and a log is one more place a
        query string could come to rest.
        """


def build_server(document: dict, port: int = 0) -> ThreadingHTTPServer:
    """A server bound to loopback, ready to `serve_forever`.

    Separate from `serve` so a test can bind, assert the address, and close
    without ever entering the accept loop.
    """
    handler = partial(DashboardHandler, document=document)
    return ThreadingHTTPServer((LOOPBACK, port), handler)


def _announce(line: str) -> None:
    """Print a startup line and flush it.

    `serve_forever` blocks for the life of the process, so a buffered stdout --
    which is what a redirected or piped run gets -- would hold the URL until the
    server was stopped. Printing an address nobody can read until they give up
    waiting for it is the same as not printing it.
    """
    print(line, flush=True)


def serve(document: dict, port: int = 0, announce=_announce) -> int:
    """Serve the page until interrupted. Returns a process exit code."""
    try:
        httpd = build_server(document, port)
    except OSError as exc:
        if port == 0:
            raise
        announce(f"port {port} is not available ({exc.strerror}); picking a free one")
        httpd = build_server(document, 0)

    host, bound = httpd.server_address[0], httpd.server_address[1]
    announce(f"http://{host}:{bound}{ROUTE}")
    announce("loopback only; this page is not reachable from any other machine")
    announce("press Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        announce("")
    finally:
        httpd.server_close()
    return 0
