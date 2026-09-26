"""Read-only local web console over the frozen ``status`` intent.

The VS Code console is the right surface for acting on code: it opens a changed
file inside the trusted workspace and reviews a diff. It is the wrong surface
for watching a queue, because durable work runs while the editor is closed and
a sidebar cannot reach the operator who has to decide something.

This server answers only that watching question. It adds no orchestration: it
calls ``status`` in its cheap workbench form and serves the already redacted
``baldr-work-item-progress`` projection. Only GET is routed, so the surface
cannot write durable state even by accident.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from .diagnostics import get_logger
from .facade import facade_status_report

_LOG = get_logger(__name__)

CONSOLE_CLIENT = "baldr-web-console"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ASSET_DIR = Path(__file__).resolve().parent / "console_assets"
_MAX_ITEM_ID = 128


def console_asset_path() -> Path:
    return _ASSET_DIR / "index.html"


def is_loopback_host(host: str) -> bool:
    return str(host).strip().lower() in _LOOPBACK_HOSTS


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
    ) -> None:
        self.workspace_root = workspace_root
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
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        self._send(body, content_type="application/json; charset=utf-8", status=status)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"ok": False, "error": {"code": code, "message": message}}, status)

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

    def _page(self) -> None:
        path = console_asset_path()
        try:
            # Read per request on purpose: editing the page and refreshing the
            # browser is the whole point of moving this surface off the VSIX
            # build, which has no watch mode.
            body = path.read_bytes()
        except OSError:
            self._error(500, "console_asset_missing", "The console page is missing.")
            return
        self._send(body, content_type="text/html; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if route in {"/", "/index.html"}:
            self._page()
            return
        if route == "/v1/workbench":
            self._workbench(query)
            return
        if route == "/livez":
            self._json({"ok": True, "client": CONSOLE_CLIENT})
            return
        self._error(404, "not_found", "Unknown console route.")

    def do_HEAD(self) -> None:
        self.do_GET()

    def _reject_write(self) -> None:
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
    )


def console_url(server: ConsoleHTTPServer) -> str:
    """Render the bound address as a URL a browser can open.

    The socket reports the host as bytes on some platforms, and an IPv6 literal
    needs brackets, so neither value can go straight into an f-string.
    """
    raw_host, port = server.server_address[0], server.server_address[1]
    host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else str(raw_host)
    if host in {"0.0.0.0", "::", ""}:
        # A wildcard bind is not an address anyone can navigate to.
        host = "127.0.0.1" if ":" not in host else "::1"
    rendered = f"[{host}]" if ":" in host else host
    return f"http://{rendered}:{int(port)}/"


def serve_console(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workspace_root: str | None = None,
    allow_non_loopback: bool = False,
) -> None:
    server = build_console_server(
        host=host,
        port=port,
        workspace_root=workspace_root,
        allow_non_loopback=allow_non_loopback,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "console": console_url(server),
                "workspace_root": workspace_root,
                "read_only": True,
            },
            ensure_ascii=False,
            indent=2,
        )
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
