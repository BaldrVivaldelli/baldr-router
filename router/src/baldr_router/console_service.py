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
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

from .agent_api import AgentContractError
from .agent_gateway import external_agent_catalog_status
from .agent_sources import manifest_from_declaration, render_declaration_toml
from .claude_cli import (
    VALID_EFFORTS as CLAUDE_EFFORTS,
    claude_model_suggestions,
    permitted_tools,
)
from .codex import codex_model_catalog
from .context7_setup import context7_runtime_status
from .diagnostics import get_logger, log_suppressed
from .discovery.inventory import attachment_record, run_git, workspace_listing
from .facade import facade_run, facade_status_report
from .process_control import install_signal_handlers
from .redaction import redact_text
from .provider_registry import get_provider_registry
from .team_resolution import role_candidates
from .work_item_progress import compact_preferences
from .durability.store import DurableStore
from .work_items import (
    RECONCILIATION_ACTION_ORDER,
    ROLE_NAMES,
    WorkItemService,
    available_execution_profiles,
    upsert_execution_profile,
    workbench_options,
)
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
_AGENTS_ROUTE = "/v1/agents"
_PROVIDERS_ROUTE = "/v1/providers"
_PROFILES_ROUTE = "/v1/profiles"
_AGENT_DRAFT_ROUTE = "/v1/agent-draft"
_API_ROUTES = frozenset(
    {"/v1/workbench", _CONTEXT_ROUTE, _AGENTS_ROUTE, _PROVIDERS_ROUTE}
)
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
# Whether Context7 has a key, whether the workspace is a trusted Git repository:
# state the configuration screen needs to tell the truth, and that only changes
# when somebody runs a command.
_ENVIRONMENT_TTL = 15.0
# The registered agents change when somebody publishes a version, which is
# rarer still, and building the catalog is the most expensive read here.
_AGENTS_TTL = 60.0
# A task points at context; it does not carry it. Each attachment costs one
# path in the prompt, and a task that needs more than this wants a narrower
# request or a directory.
_MAX_ATTACHMENTS = 25
# A phase runs its profiles in order until one succeeds. More than a handful is
# a configuration problem rather than a chain.
_MAX_ROLE_PROFILES = 12
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
# The page is read from disk on every request so it can be edited and
# refreshed, but the routes live in this process and only a restart moves them.
# Publishing them lets a page that outran its router say so, instead of
# reporting a missing feature as an unknown route.
_SERVED_ROUTES = tuple(
    sorted(
        _API_ROUTES
        | {
            _ACTION_ROUTE,
            _PREFERENCES_ROUTE,
            _COMPOSE_ROUTE,
            _PROFILES_ROUTE,
            _AGENT_DRAFT_ROUTE,
        }
    )
)
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


def describe_workspace(workspace_root: str) -> dict[str, Any]:
    """Report what the protection setting is actually protecting.

    The modes already describe themselves; what the screen could not say is the
    state they apply to. Whether this is a Git repository decides whether the
    recommended mode is even accepted, and whether the tree is dirty decides
    whether an agent's changes will land on top of unfinished ones — every mode
    the console offers writes in place, so that is a real question rather than a
    detail.
    """
    policy = inspect_workspace(workspace_root, access="write")
    git_root = policy.get("git_root")
    state: dict[str, Any] = {
        "root": workspace_root,
        "is_git_repository": bool(git_root),
        "git_root": git_root,
        "trusted": bool(policy.get("trusted")),
        "trusted_by": policy.get("trusted_by"),
        "non_git_confirmed": bool(policy.get("intentional_non_git")),
        "ready": bool(policy.get("ok")),
        "reason": policy.get("reason"),
        # None of the modes the console offers isolates writes into a copy; the
        # difference between them is whether Baldr pauses for authorization and
        # whether Git is required at all.
        "writes_in_place": True,
    }
    if not git_root:
        return state
    root = Path(workspace_root)
    branch_code, branch = run_git(root, "branch", "--show-current")
    # -uno keeps this cheap on a tree full of untracked build output; what the
    # warning is about is tracked work that is not committed yet.
    status_code, status = run_git(root, "status", "--porcelain=v1", "-uno")
    state["branch"] = branch if branch_code == 0 and branch else None
    state["dirty"] = bool(status) if status_code == 0 else None
    state["uncommitted_files"] = (
        len(status.splitlines()) if status_code == 0 and status else 0
    )
    return state


def _codex_models() -> tuple[list[dict[str, Any]], str]:
    """Return Codex's live model catalog, or nothing and why.

    Enumerating it opens an app-server session, so this only ever runs behind
    an explicit request. A provider that cannot be reached is not an error
    here: the profile can still name a model by hand.
    """
    try:
        catalog = codex_model_catalog()
    except Exception:
        log_suppressed(_LOG, "Could not list Codex models")
        return [], "unavailable"
    if not catalog.get("ok"):
        return [], "unavailable"
    models: list[dict[str, Any]] = []
    for raw in catalog.get("models") or []:
        if not isinstance(raw, Mapping):
            continue
        identifier = str(raw.get("id") or raw.get("model") or "").strip()
        if not identifier:
            continue
        models.append(
            {
                "id": identifier,
                "label": str(raw.get("display_name") or identifier),
                "description": str(raw.get("description") or "")[:240],
            }
        )
    return models[:60], "enumerated"


def describe_providers() -> dict[str, Any]:
    """Describe the providers a profile may name and what to configure on each.

    Availability travels with each one because naming an uninstalled provider
    in a profile is a mistake that only shows up when a phase runs. The models
    are suggestions, never a closed list: Codex publishes its own catalog,
    Claude documents aliases, and both accept a name typed by hand.
    """
    registry = get_provider_registry()
    status = registry.status()
    reported = status.get("providers") or {}
    providers: list[dict[str, Any]] = []
    for name in registry.canonical_names():
        health = reported.get(name) or {}
        entry: dict[str, Any] = {
            "id": name,
            "available": bool(health.get("ok")),
            "reason": str(health.get("reason") or ""),
            "enabled": health.get("enabled", True),
            "models": [],
            "model_source": "free-text",
            "efforts": [],
            # Kiro selects a named agent instead of a model, so a form that
            # only offers models would leave it unconfigurable.
            "configures": "model",
        }
        if name == "codex":
            entry["models"], entry["model_source"] = _codex_models()
            entry["efforts"] = ["low", "medium", "high"]
        elif name == "claude":
            entry["models"] = claude_model_suggestions()
            entry["model_source"] = "aliases"
            entry["efforts"] = list(CLAUDE_EFFORTS)
        elif name == "kiro-cli":
            entry["configures"] = "agent"
        providers.append(entry)
    return {
        "ok": True,
        "default_provider": str(status.get("default_provider") or ""),
        "providers": providers,
    }


def describe_agent_draft(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Say what a declared agent would mean, and write nothing.

    Drafting one here rather than in a text editor is only worth it if the
    answer comes back before the decision: which phases this agent could cover,
    and which of the tools it asked for it would actually be granted. The rules
    are the file's own, so a draft this accepts is a file that syncs.
    """
    try:
        manifest = manifest_from_declaration(entry)
    except AgentContractError as exc:
        return {"ok": True, "valid": False, "errors": [str(exc)], "toml": ""}

    provider = str(manifest.target.get("provider") or "")
    adapter = get_provider_registry().resolve(provider)
    can_write = manifest.effect_mode == "workspace-write"
    declared_tools = str(manifest.target.get("tools") or "")
    allowed, refused = permitted_tools(declared_tools, can_write=can_write)
    # One synthetic catalog entry, run through the resolver's own rules, so the
    # form cannot promise a role the run would refuse.
    catalog = {
        "agents": [
            {
                "ref": str(manifest.reference),
                "digest": manifest.digest,
                "capabilities": list(manifest.capabilities),
                "effect_mode": manifest.effect_mode,
                "enabled": True,
                "revoked": False,
                "ready": True,
                "state": "ready",
            }
        ]
    }
    return {
        "ok": True,
        "valid": True,
        "errors": [],
        "ref": str(manifest.reference),
        "digest": manifest.digest,
        "toml": render_declaration_toml(entry),
        "effect": {
            "provider": provider,
            "provider_known": adapter is not None,
            # A restriction that changes nothing is worse than none at all,
            # because it reads like protection.
            "tools_honored": bool(
                adapter is not None and adapter.capabilities.supports_tool_restriction
            ),
            "can_write": can_write,
            "declared_tools": declared_tools,
            "allowed_tools": allowed,
            "refused_tools": refused,
            "roles": [
                {
                    "role": role,
                    "eligible": bool(candidate["eligible"]),
                    "reason": str(candidate["reason"]),
                }
                for role in ROLE_NAMES
                for candidate in [role_candidates(catalog, role)[0]]
            ],
        },
    }


def describe_agents() -> dict[str, Any]:
    """Describe which registered agents could cover each phase.

    Pinning an agent is only useful if the page offers what the run will accept,
    so the standing of each one is computed with the resolver's own rules rather
    than a second opinion about capabilities.
    """
    catalog = external_agent_catalog_status()
    return {
        # The envelope's ok answers the request; the catalog's own health is a
        # separate fact, and conflating them would hide a degraded listing
        # behind what looks like a failed read.
        "ok": True,
        "catalog_ok": bool(catalog.get("ok")),
        "configured": bool(catalog.get("configured")),
        # A configured agent manager that cannot be reached: the list is real
        # but incomplete, and pinning from it would be a guess.
        "degraded": bool(catalog.get("degraded")),
        "agent_count": int(catalog.get("agent_count") or 0),
        "registry_path": str((catalog.get("local") or {}).get("path") or ""),
        "roles": {role: role_candidates(catalog, role) for role in ROLE_NAMES},
    }


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
        self._memo: dict[str, tuple[float, Any]] = {}
        self._memo_lock = threading.Lock()
        super().__init__(address, handler_class)

    def _cached(self, key: str, ttl: float, build: Callable[[], Any]) -> Any:
        """Memoize a read the page polls for but that a command alone changes.

        Everything cached here comes from the durable store, configuration, the
        secret store or Git, and none of it moves unless somebody runs
        something. What this avoids is paying for all of it several times a
        minute per open tab. The build runs outside the lock, so a slow one
        delays its own caller rather than every other reader.
        """
        now = time.monotonic()
        with self._memo_lock:
            cached = self._memo.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        fresh = build()
        with self._memo_lock:
            # Expiry is stored rather than the birth time, so entries with
            # different lifetimes can be swept by one rule.
            self._memo = {
                name: value for name, value in self._memo.items() if value[0] > now
            }
            self._memo[key] = (time.monotonic() + ttl, fresh)
        return fresh

    def workspaces(self) -> list[dict[str, Any]]:
        """Return the selectable workspaces.

        This opens a durable store to answer. Doing that per poll put an
        integrity check on a three-second path for a list that only changes when
        somebody trusts a repository or works in a new one.
        """
        return cast(
            "list[dict[str, Any]]",
            self._cached("workspaces", _WORKSPACES_TTL, selectable_workspaces),
        )

    def listing(self, workspace_root: str) -> dict[str, Any]:
        """Return this workspace's attachable paths.

        Only the picker reads this, and it is a view: whether a path may really
        be attached is decided per path when a task is composed, so a listing
        that went a few seconds stale can mislead nobody.
        """
        return cast(
            "dict[str, Any]",
            self._cached(
                f"listing:{workspace_root}",
                _LISTING_TTL,
                lambda: workspace_listing(workspace_root),
            ),
        )

    def context7(self) -> dict[str, Any]:
        """Report whether Context7 would actually contribute to a run."""

        return cast(
            "dict[str, Any]",
            self._cached("context7", _ENVIRONMENT_TTL, context7_runtime_status),
        )

    def workspace_state(self, workspace_root: str) -> dict[str, Any]:
        """Report the tree the protection setting applies to."""

        return cast(
            "dict[str, Any]",
            self._cached(
                f"workspace-state:{workspace_root}",
                _ENVIRONMENT_TTL,
                lambda: describe_workspace(workspace_root),
            ),
        )

    def providers(self) -> dict[str, Any]:
        """Return the provider catalog, deliberately outside the polled view.

        Listing Codex's models opens an app-server session and each adapter's
        status shells out, so this answers its own request like the agent
        catalog does.
        """
        return cast("dict[str, Any]", self._cached("providers", _AGENTS_TTL, describe_providers))

    def forget_providers(self) -> None:
        """Drop the cached catalog after a write that changes what it reports."""

        with self._memo_lock:
            self._memo.pop("providers", None)

    def agents(self) -> dict[str, Any]:
        """Return the agent catalog, deliberately outside the polled view.

        Building it reads the local registry, runs a diagnostic per agent and,
        when an agent manager is configured, calls it with a timeout of its own.
        A poll must never wait on that, so this answers its own request, the way
        the context listing does.
        """
        return cast("dict[str, Any]", self._cached("agents", _AGENTS_TTL, describe_agents))


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
        # never has to guess where it is pointed. Context7's state travels with
        # it too: a mode can be selected and still change nothing about a run,
        # and a screen that cannot say so is where that surprise comes from.
        self._json(
            {
                **report,
                "workspace_root": workspace_root,
                "workspace_locked": bool(self._console.workspace_root),
                "workspaces": self._console.workspaces(),
                "routes": list(_SERVED_ROUTES),
                "context7": self._console.context7(),
                "workspace_state": (
                    self._console.workspace_state(workspace_root)
                    if workspace_root
                    else None
                ),
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

    def _role_profiles(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, list[str]] | None, bool]:
        """Validate which execution profiles cover each phase.

        Every phase has to be named. ``set_preferences`` replaces the stored map
        rather than merging into it, so accepting a partial one here would let a
        screen that edits the architect silently empty the reviewer.

        The legal profile names are rebuilt from the router's configuration, not
        taken from the request, exactly as the mode fields below are.
        """
        raw = payload.get("role_profiles")
        if raw is None:
            return None, True
        if not isinstance(raw, Mapping):
            self._error(
                400,
                "invalid_role_profiles",
                "role_profiles must be an object keyed by phase.",
            )
            return None, False
        unexpected = sorted(str(key) for key in set(raw) - set(ROLE_NAMES))
        if unexpected:
            self._error(
                400, "invalid_role_profiles", "Unknown phases: " + ", ".join(unexpected)
            )
            return None, False
        known = set(available_execution_profiles().get("execution_profiles") or {})
        selected: dict[str, list[str]] = {}
        for role in ROLE_NAMES:
            values = raw.get(role)
            if not isinstance(values, list) or not values:
                self._error(
                    400,
                    "invalid_role_profiles",
                    f"Every phase needs at least one profile; {role} has none.",
                )
                return None, False
            names: list[str] = []
            for value in values[:_MAX_ROLE_PROFILES]:
                name = str(value or "").strip()
                if name not in known:
                    self._error(
                        400,
                        "invalid_role_profiles",
                        f"{name!r} is not a configured execution profile.",
                    )
                    return None, False
                if name not in names:
                    names.append(name)
            selected[role] = names
        return selected, True

    def _agent_overrides(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, str] | None, bool]:
        """Validate which registered agent is pinned to each phase.

        An empty value clears that phase's pin, and a complete map is expected
        for the same reason role_profiles is: the stored value is replaced, not
        merged. Eligibility is recomputed from the catalog, so a pin the run
        would refuse cannot be saved and discovered later.
        """
        raw = payload.get("agent_overrides")
        if raw is None:
            return None, True
        if not isinstance(raw, Mapping):
            self._error(
                400,
                "invalid_agent_overrides",
                "agent_overrides must be an object keyed by phase.",
            )
            return None, False
        unexpected = sorted(str(key) for key in set(raw) - set(ROLE_NAMES))
        if unexpected:
            self._error(
                400,
                "invalid_agent_overrides",
                "Unknown phases: " + ", ".join(unexpected),
            )
            return None, False
        catalog = self._console.agents()
        if catalog.get("degraded"):
            # The listing is real but incomplete, so an absent agent may exist.
            self._error(
                409,
                "agent_catalog_degraded",
                "The agent manager is unreachable, so the catalog is incomplete. "
                "Pinning an agent now could name a version Baldr cannot see.",
            )
            return None, False
        selected: dict[str, str] = {}
        for role in ROLE_NAMES:
            reference = str(raw.get(role) or "").strip()
            if not reference:
                continue
            eligible = {
                str(candidate["ref"])
                for candidate in catalog.get("roles", {}).get(role, [])
                if candidate.get("eligible")
            }
            if reference not in eligible:
                self._error(
                    400,
                    "invalid_agent_overrides",
                    f"{reference!r} is not a registered agent that can cover {role}.",
                )
                return None, False
            selected[role] = reference
        return selected, True

    def _profiles(self) -> None:
        """Create or replace one execution profile.

        A profile names a provider and a model. That is a smaller thing than
        trust or a credential, which stay out of the page: the worst a wrong
        profile does is run a phase on the wrong model, and fixing it is
        another save. The provider is still checked against the adapters that
        exist, so a profile cannot name one that could never run.
        """
        payload = self._read_action_request()
        if payload is None:
            return
        name = str(payload.get("name") or "").strip()
        provider = str(payload.get("provider") or "").strip()
        if not name or not provider:
            self._error(400, "invalid_profile", "A profile needs a name and a provider.")
            return
        known = {
            str(entry.get("id"))
            for entry in self._console.providers().get("providers", [])
        }
        if provider not in known:
            self._error(
                400,
                "invalid_profile",
                f"{provider!r} is not an implemented provider.",
            )
            return
        try:
            result = upsert_execution_profile(
                name,
                provider=provider,
                model=str(payload.get("model") or "").strip()[:128],
                reasoning_effort=str(payload.get("reasoning_effort") or "").strip()[:64],
                agent=str(payload.get("agent") or "").strip()[:128],
                effort=str(payload.get("effort") or "").strip()[:64],
                description=str(payload.get("description") or "").strip()[:400],
            )
        except (ValueError, OSError) as exc:
            self._error(400, "invalid_profile", redact_text(str(exc)))
            return
        # The catalog it was built from now describes the world before this.
        self._console.forget_providers()
        self._json({"ok": True, "profile": result.get("profile"), "config": result.get("config")})

    def _agent_draft(self) -> None:
        """Answer what a drafted agent would mean. Nothing here persists.

        The console is where somebody works out what an agent should be allowed
        to do; the file in their repository is where that decision lives. So
        this returns the block to put there, and never writes a catalog entry.
        """
        payload = self._read_action_request(max_bytes=_MAX_COMPOSE_BODY)
        if payload is None:
            return
        self._json(describe_agent_draft(payload))

    def _preferences(self) -> None:
        payload = self._read_action_request()
        if payload is None:
            return
        workspace_root, ok = self._scope_or_error(
            str(payload.get("workspace_root") or ""), required=True
        )
        if not ok or workspace_root is None:
            return
        role_profiles, ok = self._role_profiles(payload)
        if not ok:
            return
        agent_overrides, ok = self._agent_overrides(payload)
        if not ok:
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
        if not requested and role_profiles is None and agent_overrides is None:
            self._error(400, "invalid_preference", "No preference was supplied.")
            return
        try:
            # Named one by one rather than splatted: this is the whole surface,
            # and set_preferences keeps the current value for a None.
            preferences = WorkItemService().set_preferences(
                workspace_root,
                safety_mode=requested.get("safety_mode"),
                preset=requested.get("preset"),
                context_mode=requested.get("context_mode"),
                team_mode=requested.get("team_mode"),
                role_profiles=role_profiles,
                agent_overrides=agent_overrides,
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
            elif route == _AGENTS_ROUTE:
                self._json(self._console.agents())
            elif route == _PROVIDERS_ROUTE:
                self._json(self._console.providers())
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
        if route == _PROFILES_ROUTE:
            self._profiles()
            return
        if route == _AGENT_DRAFT_ROUTE:
            self._agent_draft()
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
