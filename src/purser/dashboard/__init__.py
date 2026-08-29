"""The dashboard: one page, one route, loopback unless a password is configured.

`server.py` holds every rule that matters -- read its module docstring before
changing anything here. `page.html`, `style.css` and `app.js` are inlined into
the single response, so the page has no subresources and renders with the
network cable pulled.
"""

from purser.dashboard.server import (
    LOOPBACK,
    ROUTE,
    USERNAME,
    AuthenticationRequired,
    build_server,
    escape_document,
    is_loopback,
    load_password,
    render_page,
    serve,
)

__all__ = [
    "LOOPBACK",
    "ROUTE",
    "USERNAME",
    "AuthenticationRequired",
    "build_server",
    "escape_document",
    "is_loopback",
    "load_password",
    "render_page",
    "serve",
]
