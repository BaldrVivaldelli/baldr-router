"""Claude Code driven headlessly as one more Baldr provider.

One role prompt goes in and one structured report comes out, the same contract
every provider answers. Two things are worth knowing about how it is driven.

A read-only phase is read-only because the tools that could write are not in
the session, not because the prompt asked nicely. ``--restricted`` removes the
command-running tools and makes the CLI ignore user, project and local settings
files, and the editing tools are denied by name on top of that. Both were
verified against the CLI: asked to create a file under these flags, the model
reports it has no way to, and no file appears.

The report is not scraped out of prose. ``--json-schema`` hands the CLI the
same report contract every other provider answers, and the result carries a
parsed ``structured_output`` beside the text. The prompt travels on stdin
because a task with its context outgrows an argument list, and because the
CLI's variadic flags would otherwise swallow a trailing positional prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .provider_errors import provider_error
from .run import run_command
from .schemas import codex_final_report_schema, normalize_final_report
from .telemetry import append_run, utc_now_iso

PROVIDER_NAME = "claude"
# The CLI's own vocabulary. Baldr's presets speak low/medium/high, and a
# profile may ask for the two the CLI adds.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Everything that edits a file. Denied by name for a read-only phase, because a
# phase that plans must not be able to change what it is planning against.
_WRITING_TOOLS = ("Write", "Edit", "NotebookEdit", "MultiEdit")
# What a read-only phase may be narrowed to. An allowlist rather than a
# denylist, so a tool nobody here has heard of is refused instead of admitted.
#
# Bash and the other code-running tools are absent because a shell writes
# whatever the phase intended, and a command pattern does not change that. Task
# is absent because a subagent is not bound by this invocation's flags, and a
# boundary that depends on inheritance nobody verified is not a boundary.
_READ_ONLY_TOOLS = frozenset(
    {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite"}
)
_MAX_TOOLS = 32
# The CLI takes an alias for the newest model in a family, or a full model
# name. These are published as suggestions rather than a closed list: a new
# family reaches the CLI before it reaches this file, and refusing it here
# would make Baldr the reason it cannot be used.
MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("opus", "Opus — el más capaz, para trabajo difícil"),
    ("sonnet", "Sonnet — equilibrio entre capacidad y costo"),
    ("haiku", "Haiku — rápido y barato, para tareas simples"),
    ("fable", "Fable — la familia más reciente"),
)


def claude_model_suggestions() -> list[dict[str, str]]:
    return [{"id": alias, "description": description} for alias, description in MODEL_ALIASES]


def claude_found(command: str | None = None) -> str | None:
    cfg = load_config()
    return shutil.which(command or cfg.claude.command)


def _credential_present(api_key_env: str) -> tuple[bool, str]:
    """Report that a credential exists, never whether it is valid.

    Validating it costs a real API call, which is the wrong price for a status
    probe that a console polls. The run itself is the authority.
    """
    if os.environ.get(api_key_env, "").strip():
        return True, f"env:{api_key_env}"
    # The interactive CLI stores an OAuth credential here after `claude login`.
    if (Path.home() / ".claude" / ".credentials.json").is_file():
        return True, "claude-login"
    return False, "none"


def claude_status() -> dict[str, Any]:
    cfg = load_config()
    path = claude_found(cfg.claude.command)
    credential, source = _credential_present(cfg.claude.api_key_env)
    result: dict[str, Any] = {
        "enabled": cfg.claude.enabled,
        "command": cfg.claude.command,
        "found": bool(path),
        "path": path,
        "model": cfg.claude.model,
        "default_effort": cfg.claude.default_effort,
        "api_key_env": cfg.claude.api_key_env,
        "credential_available": credential,
        "credential_source": source,
    }
    if path:
        version = run_command(
            [cfg.claude.command, "--version"],
            cwd=Path.cwd(),
            env=os.environ.copy(),
            timeout=15,
            stdout_limit=2048,
            stderr_limit=2048,
        )
        result["version"] = {
            "ok": version.get("ok") is True,
            "stdout": str(version.get("stdout") or "").strip()[:200],
        }
    if not path:
        result["ok"] = False
        result["reason"] = (
            f"{cfg.claude.command!r} was not found on PATH. Install Claude Code, "
            "or disable the claude provider."
        )
    elif not credential:
        result["ok"] = False
        result["reason"] = (
            "No Claude credential is available to this process. Run `claude login`, "
            f"or export {cfg.claude.api_key_env}."
        )
    else:
        result["ok"] = True
    return result


def _effort(requested: str, fallback: str) -> str:
    chosen = (requested or fallback or "").strip().lower()
    return chosen if chosen in VALID_EFFORTS else ""


def permitted_tools(requested: str, *, can_write: bool) -> tuple[list[str], list[str]]:
    """Narrow a declared tool list to what this phase may actually use.

    Returns the tools to allow and the ones refused, because a declaration that
    quietly did less than it said is worse than one that reports the difference.

    Split on commas alone: Claude accepts a pattern like ``Bash(git *)`` whose
    own spaces would not survive splitting on whitespace. The safety check
    reads the base tool before the parenthesis, so a narrowed shell is still a
    shell and is still refused for a phase that may not write.
    """
    names = [part.strip() for part in str(requested or "").split(",")]
    names = [name for name in names if name][:_MAX_TOOLS]
    if can_write:
        # Narrowing down from everything is the author's call to make.
        return list(dict.fromkeys(names)), []
    allowed: list[str] = []
    refused: list[str] = []
    for name in names:
        base = name.split("(", 1)[0].strip()
        if base in _READ_ONLY_TOOLS:
            if name not in allowed:
                allowed.append(name)
        elif name not in refused:
            refused.append(name)
    return allowed, refused


def build_claude_command(
    *,
    command: str,
    can_write: bool,
    model: str,
    effort: str,
    max_turns: int,
    report_kind: str,
    tools: str = "",
    instructions: str = "",
) -> list[str]:
    """Assemble the argument list, with the write permission decided by it.

    Kept separate from running it so the permission boundary can be asserted in
    a test without spending a model call on every assertion.

    A declared tool list travels as --allowedTools, which is a permission
    allowlist. It is never turned into --tools, which replaces the available
    set outright and would put back what --restricted removed: verified against
    the CLI, an allowlist naming Bash under --restricted still cannot run one.
    """
    cmd = [
        command,
        "--print",
        "--output-format",
        "json",
        # Nobody is at a terminal to answer a permission prompt, so one must
        # never be raised: it would hang the phase until the timeout.
        "--permission-prompts",
        "none",
        # Only MCP servers Baldr passes, never whatever the machine has
        # configured for interactive use.
        "--strict-mcp-config",
        "--json-schema",
        json.dumps(codex_final_report_schema(report_kind)),
    ]
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    if max_turns > 0:
        cmd.extend(["--max-turns", str(max_turns)])
    if can_write:
        # Edits apply without asking; the workspace mode above this decides
        # whether the phase was allowed to reach here at all.
        cmd.extend(["--permission-mode", "acceptEdits"])
    else:
        cmd.append("--restricted")
        cmd.extend(["--disallowed-tools", ",".join(_WRITING_TOOLS)])
    allowed, _refused = permitted_tools(tools, can_write=can_write)
    if allowed:
        cmd.extend(["--allowed-tools", ",".join(allowed)])
    if instructions.strip():
        # Appended, never replacing: the phase's own prompt and report contract
        # come first, and a declaration cannot talk its way out of either. It
        # also grants nothing — the flags above decide what exists to be used.
        cmd.extend(["--append-system-prompt", instructions.strip()])
    return cmd


def _report_from(payload: dict[str, Any], *, report_kind: str, text: str) -> Any:
    """Prefer the schema-validated object; fall back to the text it printed."""

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        return normalize_final_report(structured)
    # The CLI answered, but not in the shape that was asked for. Carrying the
    # prose keeps the phase reviewable instead of discarding the work.
    return {
        "status": "reviewed" if report_kind == "review" else "partial",
        "summary": text.strip()[:4000],
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
    }


def run_claude_role_prompt(
    *,
    cwd: Path,
    prompt: str,
    role: str,
    workflow: str,
    can_write: bool = False,
    model: str | None = None,
    effort: str | None = None,
    tools: str = "",
    instructions: str = "",
    report_kind: str = "review",
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    if not cfg.claude.enabled:
        return provider_error(
            "claude_disabled",
            "The claude provider is disabled. Enable it with "
            "`baldr-router enable-claude`.",
            provider=PROVIDER_NAME,
        )
    path = claude_found(cfg.claude.command)
    if not path:
        return provider_error(
            "claude_not_found",
            f"{cfg.claude.command!r} was not found on PATH. Install Claude Code.",
            provider=PROVIDER_NAME,
        )

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    selected_model = (model or cfg.claude.model or "").strip()
    selected_effort = _effort(effort or "", cfg.claude.default_effort)
    allowed_tools, refused_tools = permitted_tools(tools, can_write=can_write)
    cmd = build_claude_command(
        command=cfg.claude.command,
        can_write=can_write,
        model=selected_model,
        effort=selected_effort,
        max_turns=int(cfg.claude.max_turns),
        report_kind=report_kind,
        tools=tools,
        instructions=instructions,
    )

    started = time.time()
    started_at = utc_now_iso()
    result = run_command(
        cmd,
        cwd=cwd,
        # On stdin, not in the argument list: a task carrying its context
        # outgrows one, and a variadic flag would swallow a trailing prompt.
        stdin=prompt,
        env=env,
        timeout=int(cfg.claude.timeout_seconds),
        stdout_limit=200_000,
        stderr_limit=12_000,
    )
    duration_ms = int((time.time() - started) * 1000)

    raw = str(result.get("stdout") or "")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    text = str(payload.get("result") or raw)
    final_report = _report_from(payload, report_kind=report_kind, text=text)
    # The CLI reports its own failure in the envelope, and exit status alone
    # would call a refused turn a success.
    ok = bool(result.get("ok")) and payload.get("is_error") is not True

    out: dict[str, Any] = {
        **result,
        "ok": ok,
        "provider": PROVIDER_NAME,
        "runner": "claude-print-json",
        "role": role,
        "workflow": workflow,
        "model": selected_model,
        "effort": selected_effort,
        "can_write": can_write,
        "read_only_enforced": not can_write,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "final_report": final_report,
        "session_id": str(payload.get("session_id") or ""),
        "num_turns": payload.get("num_turns"),
        "structured": isinstance(payload.get("structured_output"), dict),
        "allowed_tools": allowed_tools,
    }
    if refused_tools:
        # Declared, and not granted. Silently running with less than the
        # manifest said would make a wrong declaration look like a working one.
        out["refused_tools"] = refused_tools
    denials = payload.get("permission_denials")
    if isinstance(denials, list) and denials:
        # A read-only phase reaching for a tool it does not have is worth
        # surfacing: it usually means the phase was given the wrong job.
        out["permission_denials"] = len(denials)
    if not ok and payload.get("is_error") is True:
        out["reason"] = text.strip()[:2000] or "Claude reported an error."
    if cfg.telemetry.enabled:
        out["telemetry_path"] = str(
            append_run(
                {
                    "run_id": f"claude-{int(started * 1000)}",
                    "ok": ok,
                    "provider": PROVIDER_NAME,
                    "runner": "claude-print-json",
                    "role": role,
                    "workflow": workflow,
                    "model": selected_model,
                    "effort": selected_effort,
                    "started_at": started_at,
                    "duration_ms": duration_ms,
                    "cwd": str(cwd),
                    "report_kind": report_kind,
                    "final_status": (
                        final_report.get("status")
                        if isinstance(final_report, dict)
                        else None
                    ),
                }
            )
        )
    return out
