"""Local web console over the frozen intents.

The VS Code console is the right surface for acting on code: it opens a changed
file inside the trusted workspace and reviews a diff. It is the wrong surface
for watching a queue, because durable work runs while the editor is closed and
a sidebar cannot reach the operator who has to decide something. So this serves
the whole loop an operator runs from outside the editor: watch a run, answer it,
configure the workspace, and ask for the next thing.

Reading stays free of side effects. Every GET goes through ``status`` in its
cheap workbench form and serves the already redacted
``baldr-work-item-progress`` projection, and settles nothing.

The writes share one rule: the page proposes and the router disposes. Nothing a
request carries is trusted as authorization for itself. ``POST /v1/actions``
recomputes which actions the item allows from durable state before it runs one.
``POST /v1/preferences`` rebuilds the legal value set rather than believing the
ids it handed out. ``POST /v1/compose`` re-checks every attached path against
the rules that produced the listing, so a stale or invented entry buys nothing.
Trust, secrets and the global config are absent by design: granting a workspace
trust is an escalation, an API key needs a different review, and config.toml
decides whether the router runs at all.

The authentication model assumes the surface writes. A localhost port is
reachable from every page the operator visits, so the defence is to carry no
ambient authority at all: the console sets no cookie, the session token lives
in the page's memory, and it travels in a header a cross-site form cannot set.
Origin, Host and Sec-Fetch-Site are checked on top of that, so a cross-site
request fails several ways before it reaches a read model.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

from .diagnostics import get_logger, log_suppressed
from .discovery.inventory import attachment_record, workspace_listing
from .facade import facade_run, facade_status_report
from .process_control import install_signal_handlers
from .redaction import redact_text
from .work_item_progress import compact_preferences
from .durability.store import DurableStore
from .work_items import RECONCILIATION_ACTION_ORDER, WorkItemService, workbench_options
from .workspace_policy import (
    WorkspacePolicyError,
    configured_trusted_roots,
    inspect_workspace,
)

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
_ACTION_ROUTE = "/v1/actions"
_PREFERENCES_ROUTE = "/v1/preferences"
_COMPOSE_ROUTE = "/v1/compose"
_CONTEXT_ROUTE = "/v1/context"
_API_ROUTES = frozenset({"/v1/workbench", _CONTEXT_ROUTE})
_MAX_TASK = 16_384
# A workflow runs for minutes, so the request that starts one cannot wait for
# it. Each start gets a worker, and this bounds how many the console will hold
# at once; the durable layer already refuses to start an item twice.
_MAX_CONCURRENT_STARTS = 4
# The list only changes when a repository is trusted or first worked in.
_WORKSPACES_TTL = 10.0
# Enumerating a repository shells out to Git, and the picker is reopened and
# refiltered far more often than a working tree changes shape.
_LISTING_TTL = 15.0
# A task points at context; it does not carry it. Each attachment costs one
# path in the prompt, and a task that needs more than this wants a narrower
# request or a directory.
_MAX_ATTACHMENTS = 25
# Each of these is a per-workspace preference whose legal values the router
# already publishes with human copy. Trust, secrets and the global config stay
# out: granting trust from a page is an escalation, an API key needs a
# different review, and config.toml decides whether the router runs at all.
_PREFERENCE_FIELDS: dict[str, str] = {
    "safety_mode": "safety_modes",
    "preset": "presets",
    "context_mode": "context_modes",
    "team_mode": "team_modes",
}
_MAX_ACTION_BODY = 4096
# A composed task is the one body that legitimately carries prose, so it gets
# room for the task text plus its attachment paths. Sharing the action limit
# capped a 16k task at 4k and reported it as an oversized request.
_MAX_COMPOSE_BODY = _MAX_TASK + 8192
# The decisions an operator makes about a run that already exists: answer a
# blocked one, or stop one. Starting and continuing work goes through the
# composer instead, because those carry a task and its context.
CONSOLE_ACTIONS = frozenset({"cancel", *RECONCILIATION_ACTION_ORDER})


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


def selectable_workspaces() -> list[dict[str, Any]]:
    """List the workspaces the console may be pointed at.

    Only places Baldr already knows: roots the operator trusted, and workspaces
    that already carry durable records. A path typed into the page is never
    resolved, so the browser can choose among these but cannot introduce one —
    granting trust stays a deliberate act at the machine.
    """
    candidates: dict[str, bool] = {}
    for root in configured_trusted_roots():
        candidates[str(root)] = True
    try:
        store = DurableStore()
        try:
            rows = store.connect().execute(
                "SELECT DISTINCT workspace_root FROM workspace_preferences "
                "UNION SELECT DISTINCT workspace_root FROM work_items"
            )
            for row in rows:
                recorded = str(row["workspace_root"] or "").strip()
                if recorded:
                    candidates.setdefault(recorded, False)
        finally:
            store.close()
    except Exception:
        log_suppressed(_LOG, "Could not read known workspaces")
    workspaces: list[dict[str, Any]] = []
    for raw, trusted in sorted(candidates.items()):
        path = Path(raw)
        if not path.is_dir():
            # A repository that moved or was deleted is not selectable.
            continue
        workspaces.append(
            {
                "root": str(path),
                "label": path.name or str(path),
                # An untrusted workspace can be watched but not worked in, and
                # saying so beats a policy error after the first click.
                "trusted": bool(trusted) or is_trusted_workspace(path),
            }
        )
    return workspaces


def is_trusted_workspace(path: Path) -> bool:
    try:
        return bool(inspect_workspace(path, access="write").get("ok"))
    except Exception:
        return False


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
        self.starts = threading.BoundedSemaphore(_MAX_CONCURRENT_STARTS)
        self._workspaces: tuple[float, list[dict[str, Any]]] | None = None
        self._workspaces_lock = threading.Lock()
        self._listings: dict[str, tuple[float, dict[str, Any]]] = {}
        self._listings_lock = threading.Lock()
        super().__init__(address, handler_class)

    def workspaces(self) -> list[dict[str, Any]]:
        """Return the selectable workspaces, recomputed at most every few seconds.

        The page polls, and this opens a durable store to answer. Doing that per
        poll put an integrity check on a hot path for a list that only changes
        when somebody trusts a repository or works in a new one.
        """
        with self._workspaces_lock:
            cached = self._workspaces
            if cached is not None and time.monotonic() - cached[0] < _WORKSPACES_TTL:
                return cached[1]
        fresh = selectable_workspaces()
        with self._workspaces_lock:
            self._workspaces = (time.monotonic(), fresh)
        return fresh

    def listing(self, workspace_root: str) -> dict[str, Any]:
        """Return this workspace's attachable paths, recomputed now and then.

        Only the picker reads this, and it is a view: whether a path may really
        be attached is decided per path when a task is composed, so a listing
        that went a few seconds stale can mislead nobody.
        """
        now = time.monotonic()
        with self._listings_lock:
            cached = self._listings.get(workspace_root)
            if cached is not None and now - cached[0] < _LISTING_TTL:
                return cached[1]
        fresh = workspace_listing(workspace_root)
        with self._listings_lock:
            self._listings = {
                root: value
                for root, value in self._listings.items()
                if now - value[0] < _LISTING_TTL
            }
            self._listings[workspace_root] = (time.monotonic(), fresh)
        return fresh


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

    def _scope(self, supplied: str | None) -> tuple[str | None, str | None]:
        """Resolve which workspace a request acts on.

        Returns the workspace and an error code, never both. A value from the
        request is matched against the selectable list rather than resolved as
        a path, so a crafted request cannot name a directory Baldr was never
        pointed at. Starting the console with --workspace-root locks the scope,
        because a window opened for one repository should stay on it.
        """
        locked = self._console.workspace_root
        wanted = (supplied or "").strip()
        if locked:
            if wanted and str(Path(wanted)) != str(Path(locked)):
                return None, "workspace_locked"
            return locked, None
        if not wanted:
            return None, None
        if wanted not in {item["root"] for item in self._console.workspaces()}:
            return None, "workspace_not_selectable"
        return wanted, None

    def _scope_or_error(
        self, supplied: str | None, *, required: bool
    ) -> tuple[str | None, bool]:
        """Return (workspace, ok). When ok is False the response was sent."""

        workspace_root, problem = self._scope(supplied)
        if problem == "workspace_locked":
            self._error(
                409,
                problem,
                "This console was started for one workspace and stays on it.",
            )
            return None, False
        if problem == "workspace_not_selectable":
            self._error(
                403,
                problem,
                "Baldr does not know that workspace. Trust it first with "
                "baldr-router trust-workspace <path>.",
            )
            return None, False
        if required and not workspace_root:
            self._error(409, "workspace_scope_required", "Choose a workspace first.")
            return None, False
        return workspace_root, True

    def _selected_item_id(self, query: Mapping[str, list[str]]) -> str | None:
        raw = (query.get("work_item_id") or [""])[0].strip()
        if not raw or len(raw) > _MAX_ITEM_ID:
            return None
        return raw

    def _workbench(self, query: Mapping[str, list[str]]) -> None:
        workspace_root, ok = self._scope_or_error(
            (query.get("workspace_root") or [""])[0], required=False
        )
        if not ok:
            return
        report = facade_status_report(
            workspace_root,
            client=CONSOLE_CLIENT,
            work_item_id=self._selected_item_id(query),
            workbench_only=True,
        )
        # The picker and the current scope travel with the view so the page
        # never has to guess where it is pointed.
        self._json(
            {
                **report,
                "workspace_root": workspace_root,
                "workspace_locked": bool(self._console.workspace_root),
                "workspaces": self._console.workspaces(),
            }
        )

    def _context(self, query: Mapping[str, list[str]]) -> None:
        """Publish the paths a task in this workspace may be pointed at.

        The page needs this to offer a choice, and offering it is all this does.
        Nothing here authorizes an attachment; composing one re-checks every
        path against the same rules, so the page cannot attach a file by
        inventing an entry that this listing never contained.
        """
        workspace_root, ok = self._scope_or_error(
            (query.get("workspace_root") or [""])[0], required=True
        )
        if not ok or workspace_root is None:
            return
        listing = self._console.listing(workspace_root)
        if not listing.get("ok"):
            self._error(
                403,
                str(listing.get("code") or "workspace_not_readable"),
                str(listing.get("reason") or "Baldr cannot read this workspace."),
            )
            return
        self._json({**listing, "max_attachments": _MAX_ATTACHMENTS})

    def _attachments(
        self, payload: Mapping[str, Any], workspace_root: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Resolve the attached paths, or answer the request and return not-ok.

        A refused path fails the whole compose rather than being dropped: a task
        that runs with less context than the operator attached is worse than one
        that does not start.
        """
        raw = payload.get("attachments")
        if raw is None or raw == []:
            return [], True
        if not isinstance(raw, list):
            self._error(
                400,
                "invalid_attachments",
                "attachments must be a list of workspace-relative paths.",
            )
            return [], False
        if len(raw) > _MAX_ATTACHMENTS:
            self._error(
                400,
                "too_many_attachments",
                f"A task can point at {_MAX_ATTACHMENTS} paths at most.",
            )
            return [], False
        attachments: list[dict[str, Any]] = []
        seen: set[str] = set()
        refused: list[str] = []
        for value in raw:
            # Accepts the plain path the page sends, and the object shape the
            # editor clients already use for the same field.
            supplied = value.get("path") if isinstance(value, Mapping) else value
            relative = str(supplied or "").strip()
            record = attachment_record(workspace_root, relative) if relative else None
            if record is None:
                refused.append(relative[:120] or "(vacío)")
                continue
            if record["label"] in seen:
                continue
            seen.add(record["label"])
            attachments.append(record)
        if refused:
            self._error(
                403,
                "attachment_not_allowed",
                "Baldr will not point a task at these paths: " + ", ".join(refused),
            )
            return [], False
        return attachments, True

    def _read_action_request(
        self, *, max_bytes: int = _MAX_ACTION_BODY
    ) -> dict[str, Any] | None:
        # A cross-site form can only send a handful of content types, none of
        # them JSON, so requiring it is one more wall before the token check
        # even matters.
        content_type = str(self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type.lower() != "application/json":
            self._error(415, "json_required", "Send application/json.")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > max_bytes:
            self._error(413, "body_too_large", "The action request is too large.")
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (OSError, ValueError):
            self._error(400, "invalid_json", "The action request is not valid JSON.")
            return None
        if not isinstance(payload, dict):
            self._error(400, "invalid_json", "The action request must be an object.")
            return None
        return payload

    def _permitted_actions(
        self, work_item_id: str, workspace_root: str | None
    ) -> set[str] | None:
        """Ask the router which actions this item allows right now.

        The page is told what to render, but nothing it sends is trusted: the
        list is recomputed here from durable state, so a crafted request can
        only ever name an action the item already permits.
        """
        report = facade_status_report(
            workspace_root,
            client=CONSOLE_CLIENT,
            work_item_id=work_item_id,
            workbench_only=True,
        )
        workbench = report.get("workbench") or {}
        selected = workbench.get("selected")
        if not isinstance(selected, dict) or workbench.get("selected_error"):
            return None
        allowed = selected.get("allowed_actions")
        return {str(action) for action in allowed} if isinstance(allowed, list) else set()

    def _run_action(
        self, work_item_id: str, action: str, workspace_root: str | None
    ) -> dict[str, Any]:
        if action == "cancel":
            return facade_run(
                workspace_root or "",
                "",
                client=CONSOLE_CLIENT,
                work_item_action="cancel-item",
                work_item_id=work_item_id,
                cancel_reason="Cancelled from the Baldr console.",
            )
        return facade_run(
            workspace_root or "",
            "",
            client=CONSOLE_CLIENT,
            work_item_action="reconcile-item",
            work_item_id=work_item_id,
            reconciliation_action=action,
        )

    def _actions(self) -> None:
        payload = self._read_action_request()
        if payload is None:
            return
        # Acting by item id alone would reach across a lock, so the scope
        # constrains the lookup the same way it constrains the view.
        workspace_root, ok = self._scope_or_error(
            str(payload.get("workspace_root") or ""), required=False
        )
        if not ok:
            return
        work_item_id = str(payload.get("work_item_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        if not work_item_id or len(work_item_id) > _MAX_ITEM_ID or not action:
            self._error(400, "invalid_action", "work_item_id and action are required.")
            return
        if action not in CONSOLE_ACTIONS:
            self._error(
                400,
                "action_not_supported",
                f"{action!r} is not one of the decisions the console offers.",
            )
            return
        permitted = self._permitted_actions(work_item_id, workspace_root)
        if permitted is None:
            self._error(404, "work_item_not_found", "No such work item.")
            return
        if action not in permitted:
            # State moved on, or the request was never legitimate. Either way
            # the durable state decides, not the page.
            self._error(
                409,
                "action_not_allowed",
                f"{action!r} is not available for this item right now.",
            )
            return
        try:
            result = self._run_action(work_item_id, action, workspace_root)
        except Exception as exc:
            log_suppressed(
                _LOG,
                "Console action failed",
                work_item_id=work_item_id,
                action=action,
            )
            self._error(500, "action_failed", redact_text(f"{type(exc).__name__}: {exc}"))
            return
        self._json({"ok": bool(result.get("ok", True)), "action": action, "result": result})

    def _preferences(self) -> None:
        payload = self._read_action_request()
        if payload is None:
            return
        workspace_root, ok = self._scope_or_error(
            str(payload.get("workspace_root") or ""), required=True
        )
        if not ok or workspace_root is None:
            return
        options = workbench_options()
        requested: dict[str, str] = {}
        for field, option_key in _PREFERENCE_FIELDS.items():
            if field not in payload:
                continue
            value = str(payload.get(field) or "").strip()
            # The page is handed these ids, but the legal set is rebuilt here
            # so a crafted body cannot introduce a mode the router never offers.
            legal = {str(item.get("id")) for item in options.get(option_key, [])}
            if value not in legal:
                self._error(
                    400,
                    "invalid_preference",
                    f"{value!r} is not a valid {field}.",
                )
                return
            requested[field] = value
        if not requested:
            self._error(400, "invalid_preference", "No preference was supplied.")
            return
        try:
            # Named one by one rather than splatted: these four are the whole
            # surface, and set_preferences keeps the current value for a None.
            preferences = WorkItemService().set_preferences(
                workspace_root,
                safety_mode=requested.get("safety_mode"),
                preset=requested.get("preset"),
                context_mode=requested.get("context_mode"),
                team_mode=requested.get("team_mode"),
                allow_non_git=bool(payload.get("allow_non_git")),
            )
        except WorkspacePolicyError as exc:
            # Choosing a mode without Git protection needs consent the page has
            # to collect, so the refusal is reported rather than worked around.
            self._error(409, exc.code or "workspace_policy", str(exc))
            return
        except (ValueError, OSError) as exc:
            self._error(400, "preference_rejected", redact_text(str(exc)))
            return
        self._json({"ok": True, "preferences": compact_preferences(preferences)})

    def _start_in_background(self, work_item_id: str) -> None:
        console = self._console

        def run() -> None:
            try:
                WorkItemService().start(work_item_id, client_name=CONSOLE_CLIENT)
            except Exception:
                # The durable record is the outcome; this only keeps the reason
                # from vanishing when nobody is watching the thread.
                log_suppressed(
                    _LOG, "Console-started work item failed", work_item_id=work_item_id
                )
            finally:
                console.starts.release()

        threading.Thread(
            target=run, name=f"baldr-console-start-{work_item_id}", daemon=True
        ).start()

    def _compose(self) -> None:
        payload = self._read_action_request(max_bytes=_MAX_COMPOSE_BODY)
        if payload is None:
            return
        workspace_root, ok = self._scope_or_error(
            str(payload.get("workspace_root") or ""), required=True
        )
        if not ok or workspace_root is None:
            return
        task = str(payload.get("task") or "").strip()
        work_item_id = str(payload.get("work_item_id") or "").strip()
        if not task:
            self._error(400, "task_required", "Write what Baldr should do.")
            return
        if len(task) > _MAX_TASK or len(work_item_id) > _MAX_ITEM_ID:
            self._error(413, "body_too_large", "The request is too long.")
            return
        attachments, allowed = self._attachments(payload, workspace_root)
        if not allowed:
            return
        if not self._console.starts.acquire(blocking=False):
            self._error(
                429,
                "too_many_starts",
                "The console is already starting as much work as it holds at once.",
            )
            return
        service = WorkItemService()
        try:
            if work_item_id:
                # A follow-up is a durable turn on the same item, which is why
                # the console needs no separate idea of a conversation.
                item = service.continue_item(
                    work_item_id,
                    workspace_root=workspace_root,
                    request=task,
                    attachments=attachments,
                    source=CONSOLE_CLIENT,
                )
            else:
                # Created without overrides on purpose: the workspace
                # preferences configured on the settings tab are the defaults.
                item = service.create(
                    workspace_root=workspace_root,
                    task=task,
                    attachments=attachments,
                    source=CONSOLE_CLIENT,
                )
        except WorkspacePolicyError as exc:
            self._console.starts.release()
            self._error(409, exc.code or "workspace_policy", str(exc))
            return
        except (KeyError, ValueError, OSError) as exc:
            self._console.starts.release()
            self._error(400, "compose_rejected", redact_text(str(exc)))
            return
        created_id = str(item["id"])
        self._start_in_background(created_id)
        # 202: the item exists and is durable, the workflow has only begun.
        self._json({"ok": True, "work_item_id": created_id, "started": True}, 202)

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
            if route == _CONTEXT_ROUTE:
                self._context(query)
            else:
                self._workbench(query)
            return
        self._error(404, "not_found", "Unknown console route.")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if not self._authorize_api():
            return
        if route == _ACTION_ROUTE:
            self._actions()
            return
        if route == _PREFERENCES_ROUTE:
            self._preferences()
            return
        if route == _COMPOSE_ROUTE:
            self._compose()
            return
        self._error(404, "not_found", "Unknown console route.")

    def _reject_write(self) -> None:
        # Authorize first so an unauthenticated caller learns nothing about
        # which verbs exist, and so the gate is already in place the day a
        # write route lands here.
        if not self._authorize_api():
            return
        self._error(
            405,
            "verb_not_supported",
            "The console answers decisions through POST /v1/actions.",
        )

    # Naming every remaining verb keeps the refusal explicit instead of relying
    # on the base class to answer 501 for whatever it does not implement.
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
    open_browser: bool = False,
) -> None:
    server = build_console_server(
        host=host,
        port=port,
        workspace_root=workspace_root,
        allow_non_loopback=allow_non_loopback,
        token=token,
    )
    # Workflows start on worker threads, and the lazy install only ever runs on
    # the main thread, so the opt-in has to happen here or a SIGTERM would
    # strand a provider's process tree.
    install_signal_handlers()
    print(
        json.dumps(
            {
                "ok": True,
                "console": console_url(server, with_token=True),
                "workspace_root": workspace_root,
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
    if open_browser:
        # The link only works with its fragment, and asking someone to copy a
        # token out of a JSON blob is the step worth removing.
        try:
            webbrowser.open(console_url(server, with_token=True))
        except Exception:
            log_suppressed(_LOG, "Could not open a browser for the console")
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
