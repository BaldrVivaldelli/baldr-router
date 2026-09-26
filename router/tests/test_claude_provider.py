"""Claude Code as a Baldr provider.

The property worth pinning hardest is the permission boundary. A phase that
plans or reviews must not be able to change what it is looking at, and here
that is true because the tools are absent from the session rather than because
the prompt discouraged their use. These tests hold the flags that make it so,
so a later convenience cannot quietly soften them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from baldr_router import claude_cli
from baldr_router.claude_cli import (
    build_claude_command,
    claude_status,
    run_claude_role_prompt,
)
from baldr_router.config import load_config, save_config
from baldr_router.provider_registry import get_provider_registry


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def enabled(isolated: None) -> None:
    cfg = load_config()
    cfg.claude.enabled = True
    cfg.telemetry.enabled = False
    save_config(cfg)


def _command(**overrides: Any) -> list[str]:
    arguments: dict[str, Any] = {
        "command": "claude",
        "can_write": False,
        "model": "",
        "effort": "",
        "max_turns": 40,
        "report_kind": "review",
    }
    arguments.update(overrides)
    return build_claude_command(**arguments)


# --- the permission boundary --------------------------------------------------


def test_a_read_only_phase_runs_without_the_tools_that_could_write() -> None:
    cmd = _command(can_write=False)

    # Removes the command-running tools, and ignores settings files that could
    # put them back.
    assert "--restricted" in cmd
    denied = cmd[cmd.index("--disallowed-tools") + 1]
    assert set(denied.split(",")) == {"Write", "Edit", "NotebookEdit", "MultiEdit"}
    assert "--permission-mode" not in cmd


def test_a_writing_phase_applies_edits_without_asking() -> None:
    cmd = _command(can_write=True)

    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    # Restricting a phase that has to write would make it fail, not make it safe.
    assert "--restricted" not in cmd
    assert "--disallowed-tools" not in cmd


def test_nobody_is_asked_to_answer_a_permission_prompt() -> None:
    """A headless run has no one at the terminal; a prompt would hang it."""

    cmd = _command()

    assert cmd[cmd.index("--permission-prompts") + 1] == "none"


def test_only_the_mcp_servers_baldr_passes_are_used() -> None:
    assert "--strict-mcp-config" in _command()


def test_the_report_contract_is_handed_to_the_cli() -> None:
    """The schema is the router's, so the report is validated at the source."""

    from baldr_router.schemas import codex_final_report_schema

    cmd = _command(report_kind="implementation")
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])

    assert schema == codex_final_report_schema("implementation")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_an_effort_the_cli_accepts_is_passed_through(effort: str) -> None:
    cmd = _command(effort=effort)

    assert cmd[cmd.index("--effort") + 1] == effort


def test_an_effort_the_cli_would_reject_is_dropped(enabled: None, monkeypatch) -> None:
    """A bad value must not become a failed invocation."""

    captured: dict[str, Any] = {}
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli, "run_command", _recorder(captured, {"ok": True, "stdout": "{}"})
    )

    run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="architect", workflow="w", effort="turbo"
    )

    assert "--effort" not in captured["cmd"]


def test_a_run_is_bounded_by_turns() -> None:
    cmd = _command(max_turns=7)

    assert cmd[cmd.index("--max-turns") + 1] == "7"


# --- how a run is driven and read --------------------------------------------


def _recorder(captured: dict[str, Any], result: dict[str, Any]):
    def fake_run_command(cmd: list[str], **kwargs: Any) -> dict[str, Any]:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return result

    return fake_run_command


def _envelope(**overrides: Any) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "session_id": "abc-123",
        "result": "text the model printed",
        "permission_denials": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_the_prompt_travels_on_stdin_not_in_the_argument_list(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task with its context outgrows an argument list.

    It is also what keeps a variadic flag from swallowing the prompt, which the
    CLI does when one trails --disallowed-tools.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli, "run_command", _recorder(captured, {"ok": True, "stdout": _envelope()})
    )
    prompt = "Revisá el diff\n" * 500

    run_claude_role_prompt(cwd=Path.cwd(), prompt=prompt, role="reviewer", workflow="w")

    assert captured["stdin"] == prompt
    assert prompt not in captured["cmd"]


def test_a_validated_report_is_used_as_the_final_report(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {"status": "approved", "summary": "todo bien", "decisions": []}
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli,
        "run_command",
        _recorder({}, {"ok": True, "stdout": _envelope(structured_output=report)}),
    )

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="reviewer", workflow="w"
    )

    assert out["ok"] is True
    assert out["structured"] is True
    assert out["final_report"]["status"] == "approved"
    # The wire shape carries decisions as pairs; Baldr stores a mapping.
    assert out["final_report"]["decisions"] == {}
    assert out["session_id"] == "abc-123"


def test_prose_is_kept_when_the_cli_returns_no_structured_report(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discarding the work because its shape is wrong helps nobody."""

    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli,
        "run_command",
        _recorder({}, {"ok": True, "stdout": _envelope(result="no pude armar el JSON")}),
    )

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="reviewer", workflow="w"
    )

    assert out["structured"] is False
    assert out["final_report"]["summary"] == "no pude armar el JSON"
    assert out["final_report"]["status"] == "reviewed"


def test_an_error_the_cli_reports_is_not_a_success(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit status alone would call a refused turn a completed one."""

    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli,
        "run_command",
        _recorder(
            {},
            {
                "ok": True,
                "stdout": _envelope(
                    is_error=True, subtype="error_max_turns", result="turn limit"
                ),
            },
        ),
    )

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="implementer", workflow="w"
    )

    assert out["ok"] is False
    assert "turn limit" in out["reason"]


def test_a_denied_tool_is_counted_for_the_operator(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli,
        "run_command",
        _recorder({}, {"ok": True, "stdout": _envelope(permission_denials=[{}, {}])}),
    )

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="architect", workflow="w"
    )

    assert out["permission_denials"] == 2


def test_output_that_is_not_json_does_not_crash_the_phase(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli, "run_command", _recorder({}, {"ok": True, "stdout": "not json at all"})
    )

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="reviewer", workflow="w"
    )

    assert out["ok"] is True
    assert out["final_report"]["summary"] == "not json at all"


# --- refusing before spending anything ----------------------------------------


def test_a_disabled_provider_refuses_without_running_anything(isolated: None) -> None:
    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="reviewer", workflow="w"
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "claude_disabled"


def test_a_missing_cli_is_reported_as_such(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: None)

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="reviewer", workflow="w"
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "claude_not_found"


def test_status_reports_a_credential_without_reading_it(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli, "run_command", _recorder({}, {"ok": True, "stdout": "2.1.0"})
    )

    status = claude_status()

    assert status["credential_available"] is True
    assert status["credential_source"] == "env:ANTHROPIC_API_KEY"
    assert "sk-ant-not-a-real-key" not in json.dumps(status)


# --- the registry -------------------------------------------------------------


def test_claude_is_a_resolvable_provider() -> None:
    registry = get_provider_registry()

    assert "claude" in registry.canonical_names()
    for alias in ("claude", "claude-code", "anthropic-claude"):
        adapter = registry.resolve(alias)
        assert adapter is not None and adapter.name == "claude"


def test_read_only_is_claimed_as_enforced_because_the_tools_are_absent() -> None:
    """Verified against the CLI, not assumed: asked to write under these flags,
    it reports it has no way to and no file appears."""

    capabilities = get_provider_registry().resolve("claude").capabilities

    assert capabilities.read_only_enforcement == "enforced"
    # Writes are scoped by the CLI's own rules rather than a sandbox, so the
    # stronger claim is not made.
    assert capabilities.write_enforcement == "advisory"
    assert capabilities.supports_workspace_write is True


def test_claude_running_inside_claude_is_blocked(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baldr is often invoked from Claude Code; that must not recurse."""

    from baldr_router.runtime_guard import provider_recursion_block_reason

    monkeypatch.setenv("BALDR_ROUTER_PARENT_PROVIDER", "claude")

    blocked = provider_recursion_block_reason("claude")

    assert blocked is not None
    assert blocked["error"]["code"] == "same_provider_recursion_blocked"


# --- a profile cannot name a provider that does not exist ---------------------


def test_a_profile_naming_an_unimplemented_provider_is_refused(
    isolated: None,
) -> None:
    """It used to be stored and fail on its first real task instead."""

    from baldr_router.work_items import upsert_execution_profile

    with pytest.raises(ValueError, match="Unknown provider"):
        upsert_execution_profile("wishful", provider="gpt-9-imaginary")


def test_a_profile_can_now_name_claude(isolated: None) -> None:
    from baldr_router.work_items import upsert_execution_profile

    result = upsert_execution_profile(
        "reviewer-claude", provider="claude", model="sonnet", reasoning_effort="high"
    )

    assert result["config"]["provider"] == "claude"
    assert load_config().execution_profiles["reviewer-claude"].model == "sonnet"


# --- what a declared agent may narrow itself to -------------------------------


@pytest.mark.parametrize(
    "requested",
    ["Bash", "Bash(git diff)", "Write", "Edit", "Task", "SomeToolNobodyKnows"],
)
def test_a_read_only_phase_refuses_a_tool_that_could_change_things(
    requested: str,
) -> None:
    """An allowlist, so an unknown tool is refused rather than admitted.

    A narrowed shell is still a shell: Bash(git diff) is refused by its base
    name, because a command pattern does not stop a redirect.
    """
    from baldr_router.claude_cli import permitted_tools

    allowed, refused = permitted_tools(requested, can_write=False)

    assert allowed == []
    assert refused == [requested]


def test_a_read_only_phase_keeps_the_tools_that_only_look() -> None:
    from baldr_router.claude_cli import permitted_tools

    allowed, refused = permitted_tools("Read, Grep ,Glob", can_write=False)

    assert allowed == ["Read", "Grep", "Glob"]
    assert refused == []


def test_a_writing_phase_narrows_itself_however_it_likes() -> None:
    """Narrowing down from everything is the author's call to make."""

    from baldr_router.claude_cli import permitted_tools

    allowed, refused = permitted_tools("Read,Edit,Bash(npm test)", can_write=True)

    assert allowed == ["Read", "Edit", "Bash(npm test)"]
    assert refused == []


def test_a_declared_tool_list_never_reopens_what_was_closed() -> None:
    """--allowedTools is a permission list; --tools replaces the tool set.

    Deriving the second from a manifest would put back what --restricted
    removed. Verified against the CLI: an allowlist naming Bash under
    --restricted still cannot run one.
    """
    cmd = _command(can_write=False, tools="Bash,Write,Read")

    assert "--tools" not in cmd
    assert cmd[cmd.index("--allowed-tools") + 1] == "Read"
    # And the closing flags survive the declaration rather than being replaced.
    assert "--restricted" in cmd
    assert "Write" in cmd[cmd.index("--disallowed-tools") + 1]


def test_no_allowlist_is_passed_when_nothing_was_declared() -> None:
    assert "--allowed-tools" not in _command(tools="")


def test_instructions_are_appended_rather_than_replacing_the_prompt() -> None:
    """The phase's own prompt and report contract come first."""

    cmd = _command(instructions="Revisá sólo la superficie pública.")

    assert cmd[cmd.index("--append-system-prompt") + 1] == "Revisá sólo la superficie pública."
    assert "--system-prompt" not in cmd


def test_a_refused_tool_is_reported_rather_than_dropped_quietly(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong declaration must not look like a working one."""

    monkeypatch.setattr(claude_cli, "claude_found", lambda *_: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli, "run_command", _recorder({}, {"ok": True, "stdout": _envelope()})
    )

    out = run_claude_role_prompt(
        cwd=Path.cwd(), prompt="x", role="reviewer", workflow="w",
        can_write=False, tools="Read,Bash",
    )

    assert out["allowed_tools"] == ["Read"]
    assert out["refused_tools"] == ["Bash"]


def test_a_manifest_carries_its_tools_to_the_provider() -> None:
    """The declarative half: target fields reach the request unchanged."""

    from baldr_router.agent_api import AgentInvocation, AgentManifest, AgentRef, ResolvedAgent
    from baldr_router.agent_gateway import ProviderAgentConnector

    manifest = AgentManifest(
        reference=AgentRef.parse("local://equipo/revisor@1.0.0"),
        owner="equipo",
        transport="provider",
        target={
            "provider": "claude",
            "model": "opus",
            "tools": "Read,Grep",
            "instructions": "Mirá sólo la superficie pública.",
        },
        capabilities=("workspace.read",),
        effect_mode="read-only",
    )
    captured: dict[str, Any] = {}

    class _Registry:
        def run(self, *, provider: str, request: Any) -> dict[str, Any]:
            captured["provider"] = provider
            captured["request"] = request
            return {"ok": True}

    ProviderAgentConnector(lambda: _Registry()).invoke(
        ResolvedAgent(manifest=manifest, source="local"),
        AgentInvocation(
            cwd=Path.cwd(), task="revisá", workflow="w", step_name="reviewer",
            report_kind="review", can_write=False, sandbox="read-only",
        ),
    )

    assert captured["provider"] == "claude"
    assert captured["request"].tools == "Read,Grep"
    assert captured["request"].instructions == "Mirá sólo la superficie pública."
    assert captured["request"].model == "opus"
