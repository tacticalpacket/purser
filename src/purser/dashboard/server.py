"""Serve the dashboard page: loopback by default, authenticated when it is not.

This page renders someone's entire financial position. The properties below are
security properties, not preferences, and each has a reason written next to it
because the reason is what a future change has to argue with.

**Loopback by default, and never routable without a password.** The listener
binds `LOOPBACK` -- the literal 127.0.0.1 -- unless a host is asked for
explicitly. Publishing this page is opt-in, because publishing it publishes the
owner's balances to every machine on the network.

Opting in does not opt out of authentication. `build_server` raises
`AuthenticationRequired` when a non-loopback host is requested with no password
configured, so the unsafe combination -- routable and unauthenticated -- cannot
be reached by any flag, in any order, rather than merely being discouraged. The
password comes from the private config home (`paths.dashboard_password_path`),
the same mechanism the rest of purser resolves private state with, and
`hmac.compare_digest` compares it so a wrong guess costs the same time whatever
its prefix. Nothing here logs the password, the `Authorization` header, or the
credentials decoded out of it: `log_message` is silenced and no code path
prints them.

Basic authentication over plain HTTP puts that password on the wire in
reversible form. On a LAN whose owner chose it that is the accepted trade; it
is not encryption and must never be described as such.

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

import base64
import binascii
import hmac
import ipaddress
import json
import re
import secrets
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from purser.core import paths

#: The address this server binds unless one is asked for explicitly.
LOOPBACK = "127.0.0.1"

#: The only account name. There is no user management here and there is not
#: going to be one; this is a single password on a single page.
USERNAME = "purser"

#: Sent with every 401 so a browser offers its own password prompt.
REALM = "purser"

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


class AuthenticationRequired(RuntimeError):
    """A routable bind was asked for with no password to put in front of it.

    Raised instead of starting, because the alternative -- serving anyway, or
    inventing a password -- publishes the whole ledger to the network. There is
    no flag that turns this off.
    """


def is_loopback(host: str) -> bool:
    """True only for an address that cannot be reached from another machine.

    Fails closed. An address literal is decided by `ipaddress`, which knows the
    whole 127.0.0.0/8 and ::1 story; the one name accepted is `localhost`.
    Anything else -- a hostname that might resolve anywhere, an empty string,
    `0.0.0.0`, `::` -- counts as routable and therefore demands a password.
    `0.0.0.0` matters most: it is not loopback, it is *every* interface.
    """
    name = (host or "").strip().strip("[]")
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def load_password() -> str | None:
    """The configured dashboard password, or None when there is no file.

    Read from the private config home like every other piece of private state
    (`paths.dashboard_password_path`); this deliberately adds no second
    configuration system. Surrounding whitespace is stripped, because a file
    written by `echo` ends in a newline and nobody types one. A file that is
    present but empty is the same as absent: there is no password, so a
    routable bind must refuse.
    """
    path = paths.dashboard_password_path()
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return secret or None


def _credentials_match(header: str | None, password: str) -> bool:
    """Whether an `Authorization` header carries the one valid credential.

    Every failure -- absent, wrong scheme, undecodable, no colon, wrong user,
    wrong password -- returns False and says nothing about which. Both halves
    are compared with `hmac.compare_digest`, and both are compared every time:
    returning early on a bad username would leak, through timing, that the
    username was the part that was wrong.
    """
    if not header:
        return False
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, sep, offered = decoded.partition(":")
    if not sep:
        return False
    user_ok = hmac.compare_digest(user, USERNAME)
    password_ok = hmac.compare_digest(offered, password)
    return user_ok and password_ok


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

    def __init__(self, *args, document: dict, password: str | None = None, **kwargs):
        self._document = document
        self._password = password
        super().__init__(*args, **kwargs)

    def _authorized(self) -> bool:
        """Whether this request may be answered at all.

        Checked before the route, so an unauthenticated client learns nothing
        about which paths exist -- 404 and 200 are both 401 to a stranger.
        `self.headers.get` is the only place the header is touched, and its
        value is never stored, printed or logged.
        """
        if self._password is None:
            return True
        return _credentials_match(self.headers.get("Authorization"), self._password)

    def _send_unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{REALM}"')
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
        if not self._authorized():
            self._send_unauthorized()
            return

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
        if not self._authorized():
            self._send_unauthorized()
            return
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


def build_server(
    document: dict,
    port: int = 0,
    host: str = LOOPBACK,
    password: str | None = None,
) -> ThreadingHTTPServer:
    """A bound server, ready to `serve_forever`.

    Separate from `serve` so a test can bind, assert the address, and close
    without ever entering the accept loop.

    The refusal happens here rather than in the CLI, before the socket exists,
    so it holds for every caller: a routable `host` with no `password` raises
    `AuthenticationRequired` and nothing is ever listening. A password given
    for a loopback bind is honoured too -- authentication is enforced whenever
    there is a password, and merely *required* when the host is routable.
    """
    if not is_loopback(host) and not password:
        raise AuthenticationRequired(
            f"refusing to serve on {host}: that address is reachable from other "
            f"machines and this page shows a complete financial position. Write "
            f"a password to {paths.dashboard_password_path()} (mode 600) and try "
            f"again. There is no way to serve a routable address without one."
        )
    handler = partial(DashboardHandler, document=document, password=password)
    return ThreadingHTTPServer((host, port), handler)


def _announce(line: str) -> None:
    """Print a startup line and flush it.

    `serve_forever` blocks for the life of the process, so a buffered stdout --
    which is what a redirected or piped run gets -- would hold the URL until the
    server was stopped. Printing an address nobody can read until they give up
    waiting for it is the same as not printing it.
    """
    print(line, flush=True)


def serve(
    document: dict,
    port: int = 0,
    host: str = LOOPBACK,
    password: str | None = None,
    announce=_announce,
) -> int:
    """Serve the page until interrupted. Returns a process exit code."""
    try:
        httpd = build_server(document, port, host=host, password=password)
    except OSError as exc:
        if port == 0:
            raise
        announce(f"port {port} is not available ({exc.strerror}); picking a free one")
        httpd = build_server(document, 0, host=host, password=password)

    bound_host, bound = httpd.server_address[0], httpd.server_address[1]
    announce(f"http://{bound_host}:{bound}{ROUTE}")
    if is_loopback(bound_host):
        announce("loopback only; this page is not reachable from any other machine")
    else:
        # Says what is true and no more. Basic auth over HTTP is not encrypted,
        # and the password itself never appears on this or any other line.
        announce("reachable from this network; HTTP Basic authentication is required")
        announce("the password is sent in reversible form -- this is not encrypted")
    announce("press Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        announce("")
    finally:
        httpd.server_close()
    return 0
