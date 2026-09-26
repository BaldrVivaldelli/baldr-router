"""Pinning a registered agent to a phase.

An external agent is an exact registered version with declared capabilities, so
a surface that offers a choice has to apply the resolver's rules or it offers a
pin the run then refuses. These tests hold that the console and the resolver
agree, and that an incomplete catalog is not treated as an empty one.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from baldr_router import console_service
from baldr_router.console_service import (
    TOKEN_HEADER,
    build_console_server,
    serve_console_in_background,
)
from baldr_router.team_resolution import role_candidates
from baldr_router.work_items import WorkItemService

TOKEN = "agents-test-token"
DIGEST = "sha256:" + "a" * 64
READER = "local://pilot/reader@1.0.0"
WRITER = "local://pilot/writer@1.0.0"


def _agent(reference: str, *, capabilities: tuple[str, ...], **extra: object) -> dict:
    return {
        "ref": reference,
        "digest": DIGEST,
        "owner": "pilot-team",
        "capabilities": list(capabilities),
        "effect_mode": "read-only",
        "state": "ready",
        "ready": True,
        "enabled": True,
        "revoked": False,
        "source": "local",
        **extra,
    }


def _catalog(*agents: dict, degraded: bool = False) -> dict:
    return {
        "ok": not degraded,
        "configured": True,
        "degraded": degraded,
        "agent_count": len(agents),
        "agents": list(agents),
        "local": {"path": "/tmp/agents.json"},
    }


READ_ONLY_AGENT = _agent(READER, capabilities=("workspace.read",))
WRITING_AGENT = _agent(
    WRITER,
    capabilities=("workspace.read", "workspace.write"),
    effect_mode="workspace-write",
)


# --- the rules, shared with the resolver --------------------------------------


def test_a_read_only_agent_can_plan_and_review_but_not_execute() -> None:
    catalog = _catalog(READ_ONLY_AGENT)

    assert role_candidates(catalog, "architect")[0]["eligible"] is True
    assert role_candidates(catalog, "reviewer")[0]["eligible"] is True

    execution = role_candidates(catalog, "implementer")[0]
    assert execution["eligible"] is False
    # The reason is the useful half of the refusal.
    assert "escritura" in execution["reason"]


def test_a_writing_agent_can_cover_every_phase() -> None:
    catalog = _catalog(WRITING_AGENT)

    for role in ("architect", "implementer", "reviewer"):
        assert role_candidates(catalog, role)[0]["eligible"] is True


def test_an_agent_that_does_not_read_the_workspace_is_never_eligible() -> None:
    catalog = _catalog(_agent("local://pilot/blind@1.0.0", capabilities=()))

    for role in ("architect", "implementer", "reviewer"):
        candidate = role_candidates(catalog, role)[0]
        assert candidate["eligible"] is False
        assert candidate["reason"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("revoked", True), ("enabled", False), ("ready", False), ("digest", "nope")],
)
def test_an_unusable_version_is_ruled_out_with_its_reason(
    field: str, value: object
) -> None:
    catalog = _catalog(
        _agent(READER, capabilities=("workspace.read",), **{field: value})
    )

    candidate = role_candidates(catalog, "architect")[0]

    assert candidate["eligible"] is False
    assert candidate["reason"]


def test_usable_agents_are_listed_first() -> None:
    catalog = _catalog(
        _agent("local://pilot/zz-good@1.0.0", capabilities=("workspace.read",)),
        _agent("local://pilot/aa-bad@1.0.0", capabilities=()),
    )

    listed = role_candidates(catalog, "architect")

    assert [entry["eligible"] for entry in listed] == [True, False]


def test_an_unknown_role_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        role_candidates(_catalog(), "deployer")


# --- the console surface ------------------------------------------------------


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=T", "-c",
         "user.email=t@example.invalid", "commit", "-qm", "initial"],
        check=True,
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(tmp_path)])
    )
    return _repo(tmp_path / "repo")


@pytest.fixture
def console(workspace: Path) -> Iterator[str]:
    server = build_console_server(
        host="127.0.0.1", port=0, token=TOKEN, workspace_root=str(workspace)
    )
    serve_console_in_background(server)
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def two_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        console_service,
        "external_agent_catalog_status",
        lambda **_: _catalog(READ_ONLY_AGENT, WRITING_AGENT),
    )


@pytest.fixture
def degraded_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        console_service,
        "external_agent_catalog_status",
        lambda **_: _catalog(READ_ONLY_AGENT, degraded=True),
    )


def _get(console: str, path: str, *, token: str | None = TOKEN) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(f"{console}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _post(console: str, body: object) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{console}/v1/preferences",
        data=json.dumps(body).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def test_the_catalog_is_published_per_phase(console: str, two_agents: None) -> None:
    status, payload = _get(console, "/v1/agents")

    assert status == 200, payload
    assert payload["agent_count"] == 2
    planners = {entry["ref"] for entry in payload["roles"]["architect"] if entry["eligible"]}
    executors = {entry["ref"] for entry in payload["roles"]["implementer"] if entry["eligible"]}
    assert planners == {READER, WRITER}
    # The read-only one cannot execute, and the page is told why rather than
    # simply not offered it.
    assert executors == {WRITER}


def test_the_catalog_needs_the_session_token(console: str) -> None:
    status, payload = _get(console, "/v1/agents", token=None)

    assert status == 401
    assert payload["error"]["code"] == "console_token_required"


def test_an_empty_registry_says_so_rather_than_failing(console: str) -> None:
    """Nothing registered is a normal state, not an error."""

    status, payload = _get(console, "/v1/agents")

    assert status == 200
    assert payload["ok"] is True
    assert payload["agent_count"] == 0
    assert payload["roles"]["architect"] == []


def test_pinning_an_eligible_agent_is_saved(
    console: str, workspace: Path, two_agents: None
) -> None:
    status, payload = _post(
        console,
        {"agent_overrides": {"architect": READER, "implementer": WRITER, "reviewer": ""}},
    )

    assert status == 200, payload
    stored = WorkItemService().preferences(str(workspace))["agent_overrides"]
    assert stored == {"architect": READER, "implementer": WRITER}


def test_pinning_an_agent_to_a_phase_it_cannot_cover_is_refused(
    console: str, workspace: Path, two_agents: None
) -> None:
    status, payload = _post(console, {"agent_overrides": {"implementer": READER}})

    assert status == 400
    assert payload["error"]["code"] == "invalid_agent_overrides"
    assert WorkItemService().preferences(str(workspace))["agent_overrides"] == {}


def test_pinning_an_unregistered_agent_is_refused(
    console: str, two_agents: None
) -> None:
    status, payload = _post(
        console, {"agent_overrides": {"architect": "local://pilot/ghost@9.9.9"}}
    )

    assert status == 400
    assert payload["error"]["code"] == "invalid_agent_overrides"


def test_an_invented_phase_is_refused(console: str, two_agents: None) -> None:
    status, payload = _post(console, {"agent_overrides": {"deployer": READER}})

    assert status == 400
    assert payload["error"]["code"] == "invalid_agent_overrides"


def test_a_pin_can_be_cleared(console: str, workspace: Path, two_agents: None) -> None:
    _post(console, {"agent_overrides": {"architect": READER}})
    assert WorkItemService().preferences(str(workspace))["agent_overrides"]

    status, _ = _post(
        console,
        {"agent_overrides": {"architect": "", "implementer": "", "reviewer": ""}},
    )

    assert status == 200
    assert WorkItemService().preferences(str(workspace))["agent_overrides"] == {}


def test_an_incomplete_catalog_refuses_to_pin(
    console: str, workspace: Path, degraded_catalog: None
) -> None:
    """An unreachable agent manager means an absent agent may still exist."""

    status, payload = _post(console, {"agent_overrides": {"architect": READER}})

    assert status == 409
    assert payload["error"]["code"] == "agent_catalog_degraded"
    assert WorkItemService().preferences(str(workspace))["agent_overrides"] == {}


def test_a_degraded_catalog_is_reported_rather_than_hidden(
    console: str, degraded_catalog: None
) -> None:
    status, payload = _get(console, "/v1/agents")

    # The request succeeded; the catalog behind it did not.
    assert status == 200
    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert payload["catalog_ok"] is False


def test_the_catalog_is_not_built_on_the_polled_view(
    console: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll must never wait on a remote agent manager's timeout."""

    calls: list[int] = []

    def counted(**_: object) -> dict:
        calls.append(1)
        return _catalog()

    monkeypatch.setattr(console_service, "external_agent_catalog_status", counted)

    _get(console, "/v1/workbench")
    _get(console, "/v1/workbench")

    assert calls == []

    _get(console, "/v1/agents")
    assert len(calls) == 1


def test_the_page_says_when_nothing_is_registered() -> None:
    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    assert "No hay agentes externos registrados" in page
    assert "baldr-router agent publish" in page
    # A pin beats automatic selection, which is worth stating once.
    assert "manda incluso" in page


# --- drafting one, without writing anything -----------------------------------


def _draft(console: str, body: object) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{console}/v1/agent-draft",
        data=json.dumps(body).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


REVIEWER_DRAFT = {
    "ref": "local://equipo/revisor@1.0.0",
    "owner": "equipo-plataforma",
    "description": "Revisa la superficie pública",
    "capabilities": ["workspace.read", "role.reviewer"],
    "effect_mode": "read-only",
    "provider": "claude",
    "model": "opus",
    "tools": "Read,Grep,Bash",
    "instructions": "Mirá autenticación.",
}


def test_a_draft_says_which_phases_it_could_cover(console: str) -> None:
    """Computed with the resolver's rules, so the form cannot promise a role
    the run would refuse."""

    status, payload = _draft(console, REVIEWER_DRAFT)

    assert status == 200, payload
    assert payload["valid"] is True
    eligible = {
        entry["role"] for entry in payload["effect"]["roles"] if entry["eligible"]
    }
    assert eligible == {"reviewer"}
    # And says why not, rather than leaving a blank.
    refused = {
        entry["role"]: entry["reason"]
        for entry in payload["effect"]["roles"]
        if not entry["eligible"]
    }
    assert refused["implementer"]
    assert refused["architect"]


def test_a_draft_says_which_tools_it_would_actually_get(console: str) -> None:
    _, payload = _draft(console, REVIEWER_DRAFT)

    effect = payload["effect"]
    assert effect["allowed_tools"] == ["Read", "Grep"]
    assert effect["refused_tools"] == ["Bash"]
    assert effect["tools_honored"] is True


def test_the_declaration_keeps_what_was_asked_for(console: str) -> None:
    """The file records intent; the router applies what it grants.

    Rewriting the block to drop a refused tool would make the file lie about
    what somebody meant, and would teach them nothing about why.
    """
    _, payload = _draft(console, REVIEWER_DRAFT)

    assert 'tools = "Read,Grep,Bash"' in payload["toml"]


def test_a_provider_that_ignores_tools_says_so(console: str) -> None:
    """A restriction that changes nothing reads like protection."""

    _, payload = _draft(console, {**REVIEWER_DRAFT, "provider": "codex"})

    assert payload["effect"]["tools_honored"] is False


def test_an_invalid_draft_answers_with_its_reason(console: str) -> None:
    status, payload = _draft(
        console,
        {
            "ref": "local://equipo/x@1.0.0",
            "owner": "e",
            "provider": "claude",
            "capabilities": ["workspace.read"],
            "effect_mode": "workspace-write",
        },
    )

    assert status == 200
    assert payload["valid"] is False
    assert "workspace.write" in payload["errors"][0]


def test_drafting_writes_nothing(console: str) -> None:
    """The console is where the decision is worked out, not where it lives."""

    from baldr_router.agent_registry import agent_registry_status

    _draft(console, REVIEWER_DRAFT)

    assert agent_registry_status()["agent_count"] == 0


def test_drafting_requires_the_session_token(console: str) -> None:
    request = urllib.request.Request(
        f"{console}/v1/agent-draft",
        data=json.dumps(REVIEWER_DRAFT).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=30)

    assert caught.value.code == 401


def test_a_block_the_console_accepts_is_a_file_that_syncs(
    console: str, tmp_path: Path
) -> None:
    """One format, one opinion.

    A console that showed a draft as valid and a sync that then refused it
    would be two answers about the same file, and the second one arrives after
    somebody committed the first.
    """
    from baldr_router.agent_sources import AgentSourceContext, DeclarativeAgentSource

    _, payload = _draft(console, REVIEWER_DRAFT)
    (tmp_path / "baldr-agents.toml").write_text(payload["toml"], encoding="utf-8")

    result = DeclarativeAgentSource(path=Path("baldr-agents.toml")).discover(
        context=AgentSourceContext(tmp_path)
    )

    assert len(result.candidates) == 1
    # The same bytes describe the same agent, down to its identity.
    assert result.candidates[0].manifest.digest == payload["digest"]
