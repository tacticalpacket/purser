"""Two properties of the dashboard page that a mistake would quietly break.

Neither is a style check. Each fails only if the page has actually stopped
doing its job, and each is written against behaviour -- the page as an HTML
parser sees it, and the socket as the operating system reports it -- rather
than against the shape of the source.
"""

from __future__ import annotations

import json
import socket
import threading
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path

import pytest

from purser.dashboard import server as dashboard_server

FIXTURE = Path(__file__).parent / "fixtures" / "dashboard_sample.json"

#: The two merchant groups in the fixture that exist to be hostile. The first
#: closes a script element and opens another; the second is the ordinary case
#: that a naive HTML-escaping pass gets wrong in the opposite direction, by
#: double-escaping an ampersand a bank really did write.
BREAKOUT_GROUP = "</script><script>alert('xss')</script> Diner"
AMPERSAND_GROUP = "Ampersand & Sons Hardware"


@pytest.fixture
def document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class PageParser(HTMLParser):
    """Reads the page the way a browser's tokenizer does.

    Records every element the parser is willing to create and the raw text of
    each `<script>` element's contents. If a merchant description ever escapes
    its data block, a new element appears here -- which is the failure, stated
    in the terms that make it dangerous.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.script_types: list[str] = []
        self._in_script = False
        self.script_bodies: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "script":
            self.script_types.append(dict(attrs).get("type", "javascript"))
            self._in_script = True
            self.script_bodies.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_bodies[-1] += data


def test_hostile_merchant_names_stay_inert_text(document):
    """A description that closes a script element must not become markup.

    Merchant descriptions arrive from an institution and nobody sanitised them.
    The fixture carries one that closes a `<script>` and opens another, and one
    that merely contains an ampersand -- the two mistakes point in opposite
    directions, so both are checked here.

    The page is parsed rather than searched. Three things have to hold: the
    parser finds exactly the two script elements the page ships with, so the
    payload created no element of its own; the data block still parses as JSON,
    so the escaping did not corrupt the document; and the values that come back
    out are the original strings, character for character, so the escaping did
    not mangle a real merchant's name either.
    """
    groups = [m["group"] for m in document["top_merchants"]]
    assert BREAKOUT_GROUP in groups and AMPERSAND_GROUP in groups, (
        "the fixture must keep carrying the hostile names this test exists for"
    )

    page = dashboard_server.render_page(document, nonce="test-nonce")
    parser = PageParser()
    parser.feed(page)

    # 1. Nothing new was created. The page ships exactly two script elements:
    #    the JSON data block and the renderer. A breakout would add a third.
    assert parser.script_types == ["application/json", "javascript"], (
        f"expected exactly the data block and the renderer, got {parser.script_types}"
    )
    tags = [tag for tag, _ in parser.elements]
    assert "img" not in tags, "the fixture's <img onerror=...> payload became an element"

    # 2. The data block still round-trips as JSON.
    data_block = parser.script_bodies[0]
    assert "<" not in data_block and ">" not in data_block and "&" not in data_block, (
        "an unescaped <, > or & is sitting in the data block, so a description "
        "one character longer could close it"
    )
    parsed = json.loads(data_block)

    # 3. And it round-trips to the same strings, hostile ones included.
    assert parsed == document
    round_tripped = [m["group"] for m in parsed["top_merchants"]]
    assert BREAKOUT_GROUP in round_tripped
    assert AMPERSAND_GROUP in round_tripped, (
        "the ampersand was altered on the way through; a bank's own name for a "
        "merchant must survive the escaping unchanged"
    )


def test_inlined_assets_cannot_truncate_their_own_element():
    """The renderer's source must survive being inlined into `<script>`.

    An HTML parser ends a script element at the first `</script`, wherever it
    lands -- inside a string, inside a comment, anywhere. `app.js`'s header
    comment discusses the `</script>` breakout it defends against, and before
    `inline_safely` existed that sentence truncated the element and the whole
    renderer failed to parse, silently, with the page still returning 200.

    The evidence is the parsed body, not the source: the renderer's last line
    has to still be inside the script element the browser sees.
    """
    page = dashboard_server.render_page({"coverage": {}}, nonce="test-nonce")
    parser = PageParser()
    parser.feed(page)

    assert parser.script_types == ["application/json", "javascript"]
    renderer = parser.script_bodies[1]
    assert "function main()" in renderer, "the renderer was truncated before its entry point"
    assert renderer.rstrip().endswith("})();"), (
        "the renderer's closing line is missing, so the script element ended early"
    )


def test_the_server_binds_loopback_and_only_loopback(document):
    """The listener must not be reachable from another machine.

    This page has no authentication of any kind in front of a complete picture
    of someone's finances, so the bind address is the whole access-control
    story. A bind to 0.0.0.0 -- one word's difference -- publishes it to every
    machine on the network, and would still pass every other test in this file.

    Asked of the socket, not of the source: a 0.0.0.0 bind reports `0.0.0.0`
    here, and a bind to a routable address reports that address.
    """
    httpd = dashboard_server.build_server(document, port=0)
    try:
        host, port = httpd.server_address[0], httpd.server_address[1]
        assert host == "127.0.0.1"
        assert httpd.socket.getsockname()[0] == "127.0.0.1"
        assert dashboard_server.LOOPBACK == "127.0.0.1"

        # A routable address of this host, if it has one, must refuse the
        # connection: the socket is not listening there.
        try:
            routable = socket.gethostbyname(socket.gethostname())
        except OSError:
            routable = None
        if routable and not routable.startswith("127."):
            probe = socket.socket()
            probe.settimeout(2)
            with pytest.raises(OSError):
                probe.connect((routable, port))
            probe.close()
    finally:
        httpd.server_close()


def test_the_one_route_answers_and_carries_its_headers(document):
    """`GET /` returns the page; everything else is a 404 with no body.

    There is no static-file handler behind this server, so there is no path to
    traverse -- but that is a claim worth failing loudly if someone adds one.
    The response headers are checked here too, because `no-store` and a CSP
    with no remote origin are what keep the page off disk and off the network.
    """
    httpd = dashboard_server.build_server(document, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[0], httpd.server_address[1]

        con = HTTPConnection(host, port, timeout=5)
        con.request("GET", "/")
        response = con.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"

        csp = response.getheader("Content-Security-Policy")
        assert "default-src 'none'" in csp
        assert "http://" not in csp and "https://" not in csp, (
            "the policy names a remote origin; this page must load nothing remote"
        )
        assert "'unsafe-inline'" not in csp, (
            "'unsafe-inline' would let an escaping mistake execute instead of failing closed"
        )
        # The page ships with no subresources at all, which is what lets it
        # render with the network cable pulled.
        assert 'src="' not in body
        assert 'href="#' in body and 'href="http' not in body
        con.close()

        con = HTTPConnection(host, port, timeout=5)
        con.request("GET", "/../../etc/passwd")
        response = con.getresponse()
        assert response.status == 404
        assert response.read() == b""
        con.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
