"""Read-only local web console over the frozen ``status`` intent.

The VS Code console is the right surface for acting on code: it opens a changed
file inside the trusted workspace and reviews a diff. It is the wrong surface
for watching a queue, because durable work runs while the editor is closed and
a sidebar cannot reach the operator who has to decide something.

This server answers only that watching question. It adds no orchestration: it
calls ``status`` in its cheap workbench form and serves the already redacted
``baldr-work-item-progress`` projection. Only GET is routed today, so the
surface cannot write durable state even by accident.

The authentication model is built for the surface that will write. A localhost
port is reachable from every page the operator visits, so the defence is to
carry no ambient authority at all: the console sets no cookie, the session
token lives in the page's memory, and it travels in a header a cross-site form
cannot set. Origin, Host and Sec-Fetch-Site are checked on top of that, so a
cross-site request fails several ways before it reaches a read model.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

from .diagnostics import get_logger
from .facade import facade_status_report

_LOG = get_logger(__name__)

CONSOLE_CLIENT = "baldr-web-console"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
TOKEN_ENV = "BALDR_CONSOLE_TOKEN"
TOKEN_HEADER = "X-Baldr-Console-Token"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})
_ASSET_DIR = Path(__file__).resolve().parent / "console_assets"
_MAX_ITEM_ID = 128
# Served verbatim; every one is authored in this repository and none is a path
# the request can influence.
_PUBLIC_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
_API_ROUTES = frozenset({"/v1/workbench"})


def console_asset_path(name: str = "index.html") -> Path:
    return _ASSET_DIR / name


def is_loopback_host(host: str) -> bool:
    return str(host).strip().lower() in _LOOPBACK_HOSTS


def bound_address(server: ConsoleHTTPServer) -> tuple[str, int]:
    """Return the bound host and port as plain values.

    ``server_address`` is loosely typed and reports the host as bytes on some
    platforms, so every caller that reasons about the address goes through here
    rather than repeating the decode.
    """
    address = cast("tuple[Any, ...]", server.server_address)
    raw_host, port = address[0], address[1]
    host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else str(raw_host)
    return host, int(port)


def new_console_token() -> str:
    """Return the session token, preferring one the operator pinned.

    A stable token lets somebody bookmark the console or drive it from a
    script; without one every start issues a fresh secret.
    """

    configured = os.environ.get(TOKEN_ENV, "").strip()
    return configured or secrets.token_urlsafe(32)


class ConsoleHTTPServer(ThreadingHTTPServer):
    """Server owning the workspace scope the console reports on."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        workspace_root: str | None = None,
        token: str | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.token = token or new_console_token()
        super().__init__(address, handler_class)


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    """Serve the console page and the one read model it renders."""

    server_version = "BaldrConsole/1"
    protocol_version = "HTTP/1.1"

    @property
    def _console(self) -> ConsoleHTTPServer:
        return cast(ConsoleHTTPServer, self.server)

    def _send(self, body: bytes, *, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page renders only its own payload and loads nothing remote.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src 'self'; manifest-src 'self'; "
            "connect-src 'self'; worker-src 'self'; "
            # Nothing on this page posts or navigates, so both are denied
            # outright rather than left to the default.
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        self._send(body, content_type="application/json; charset=utf-8", status=status)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"ok": False, "error": {"code": code, "message": message}}, status)

    def _expected_hosts(self) -> set[str]:
        host, port = bound_address(self._console)
        names = {host, "localhost", "127.0.0.1", "[::1]", "::1"}
        return {f"{name}:{port}" for name in names} | names

    def _host_is_expected(self) -> bool:
        """Reject DNS rebinding, where an attacker's name resolves to loopback.

        The browser would then send that attacker name in Host and treat the
        response as same-origin, so the server has to refuse a name it does not
        answer for. A wildcard bind cannot enumerate the names it answers on, so
        there the Origin and token checks carry the request alone.
        """
        supplied = str(self.headers.get("Host") or "").strip().lower()
        if not supplied:
            return False
        host, _ = bound_address(self._console)
        if host in _WILDCARD_HOSTS:
            return True
        return supplied in {name.lower() for name in self._expected_hosts()}

    def _origin_is_same(self) -> bool:
        """Allow only a request with no Origin, or one naming this server.

        A same-origin GET usually omits Origin. Anything a page on another site
        initiates carries one, and that is exactly what must not pass.
        """
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        return parsed.netloc.lower() in {name.lower() for name in self._expected_hosts()}

    def _fetch_site_is_same(self) -> bool:
        site = str(self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        # Older clients omit the header; the token still gates the request.
        return site in {"", "same-origin", "none"}

    def _token_is_valid(self) -> bool:
        supplied = str(self.headers.get(TOKEN_HEADER) or "")
        if not supplied:
            return False
        return hmac.compare_digest(supplied, self._console.token)

    def _authorize_api(self) -> bool:
        """Answer every API request that is not provably first-party."""

        if not self._host_is_expected():
            self._error(403, "host_not_allowed", "Unexpected Host header.")
            return False
        if not self._origin_is_same():
            self._error(403, "cross_origin", "Cross-origin requests are refused.")
            return False
        if not self._fetch_site_is_same():
            self._error(403, "cross_site", "Cross-site requests are refused.")
            return False
        if not self._token_is_valid():
            self._error(
                401,
                "console_token_required",
                f"Send the session token in the {TOKEN_HEADER} header. "
                "Open the console with the link the command printed.",
            )
            return False
        return True

    def _selected_item_id(self, query: Mapping[str, list[str]]) -> str | None:
        raw = (query.get("work_item_id") or [""])[0].strip()
        if not raw or len(raw) > _MAX_ITEM_ID:
            return None
        return raw

    def _workbench(self, query: Mapping[str, list[str]]) -> None:
        # The workspace scope is fixed when the server starts. Taking it from the
        # query string would let a request name any path on disk.
        report = facade_status_report(
            self._console.workspace_root,
            client=CONSOLE_CLIENT,
            work_item_id=self._selected_item_id(query),
            workbench_only=True,
        )
        self._json(report)

    def _asset(self, name: str, content_type: str) -> None:
        try:
            # Read per request on purpose: editing the page and refreshing the
            # browser is the whole point of moving this surface off the VSIX
            # build, which has no watch mode.
            body = console_asset_path(name).read_bytes()
        except OSError:
            self._error(500, "console_asset_missing", f"{name} is missing.")
            return
        self._send(body, content_type=content_type)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        asset = _PUBLIC_ASSETS.get(route)
        if asset is not None:
            # The shell carries no data, so serving it unauthenticated keeps a
            # bookmark working and lets the page explain a missing token.
            self._asset(*asset)
            return
        if route == "/livez":
            self._json({"ok": True, "client": CONSOLE_CLIENT})
            return
        if route in _API_ROUTES:
            if not self._authorize_api():
                return
            self._workbench(query)
            return
        self._error(404, "not_found", "Unknown console route.")

    def do_HEAD(self) -> None:
        self.do_GET()

    def _reject_write(self) -> None:
        # Authorize first so an unauthenticated caller learns nothing about
        # which verbs exist, and so the gate is already in place the day a
        # write route lands here.
        if not self._authorize_api():
            return
        self._error(
            405,
            "read_only_console",
            "The console is read-only. Run work from the CLI or the editor.",
        )

    # Naming every write verb keeps the refusal explicit instead of relying on
    # the base class to answer 501 for whatever it does not implement.
    do_POST = _reject_write
    do_PUT = _reject_write
    do_PATCH = _reject_write
    do_DELETE = _reject_write

    def log_message(self, format: str, *args: Any) -> None:
        # Access lines belong in the redacted diagnostics log, never on stdout,
        # which the MCP server owns in other entrypoints.
        _LOG.debug("console %s", format % args if args else format)


def build_console_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workspace_root: str | None = None,
    allow_non_loopback: bool = False,
    token: str | None = None,
) -> ConsoleHTTPServer:
    """Bind the console, refusing a public bind unless it was asked for.

    The payload is redacted but still describes real work: task text, relative
    paths and findings. Reaching the console from a phone is a feature, so the
    non-loopback bind stays available and deliberate rather than accidental.
    """

    if not is_loopback_host(host) and not allow_non_loopback:
        raise ValueError(
            f"Refusing to serve the console on {host!r} without "
            "allow_non_loopback; it exposes workspace activity to the network."
        )
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ConsoleHTTPServer.address_family = family
    return ConsoleHTTPServer(
        (host, int(port)),
        ConsoleRequestHandler,
        workspace_root=workspace_root,
        token=token,
    )


def console_url(server: ConsoleHTTPServer, *, with_token: bool = False) -> str:
    """Render the bound address as a URL a browser can open.

    The socket reports the host as bytes on some platforms, and an IPv6 literal
    needs brackets, so neither value can go straight into an f-string.
    """
    host, port = bound_address(server)
    if host in _WILDCARD_HOSTS:
        # A wildcard bind is not an address anyone can navigate to.
        host = "127.0.0.1" if ":" not in host else "::1"
    rendered = f"[{host}]" if ":" in host else host
    base = f"http://{rendered}:{port}/"
    if not with_token:
        return base
    # The token rides in the fragment, which browsers never put in a request
    # line, a Referer header or a server log. The page reads it once and clears
    # it from the address bar.
    return f"{base}#token={quote(server.token, safe='')}"


def serve_console(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workspace_root: str | None = None,
    allow_non_loopback: bool = False,
    token: str | None = None,
) -> None:
    server = build_console_server(
        host=host,
        port=port,
        workspace_root=workspace_root,
        allow_non_loopback=allow_non_loopback,
        token=token,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "console": console_url(server, with_token=True),
                "workspace_root": workspace_root,
                "read_only": True,
                "token_env": TOKEN_ENV,
            },
            ensure_ascii=False,
            indent=2,
        ),
        # This line carries the token, and the process then blocks forever. A
        # redirected stdout is block-buffered, so without an explicit flush the
        # operator never receives the link they need.
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def serve_console_in_background(
    server: ConsoleHTTPServer,
) -> threading.Thread:
    """Run an already bound console server on a daemon thread."""

    thread = threading.Thread(
        target=server.serve_forever, name="baldr-console", daemon=True
    )
    thread.start()
    return thread
