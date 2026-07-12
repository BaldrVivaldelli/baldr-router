from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .config import RoleConfig, load_config
from .context7 import prepare_context7_bundle
from .execution_profiles import resolve_role_profiles
from .provider_registry import run_provider_role
from .runtime_guard import child_provider_env, new_run_id, reentry_block_reason
from .workspace_policy import WorkspacePolicyError, require_workspace

DIRECT_TASK_WORKFLOW = "direct-task"
DIRECT_REVIEW_WORKFLOW = "direct-review"


def _workspace(workspace_root: str, *, access: str) -> Path:
    return require_workspace(workspace_root, access=access)


def _context7_note(
    *,
    workspace_root: Path,
    task_text: str,
    context7_libraries: list[str] | None,
) -> tuple[str, dict[str, Any]]:
    cfg = load_config()
    bundle = prepare_context7_bundle(
        workspace_root=workspace_root,
        task_text=task_text,
        libraries=context7_libraries,
    )
    if bundle.get("used"):
        note = (
            "Context7 documentation was prefetched and cached by baldr-router. "
            "Treat it as supporting reference material; project code and tests win if they disagree.\n\n"
            + str(bundle.get("bundle", ""))
        )
    elif cfg.context7.enabled and cfg.context7.mode in {"codex-mcp", "hybrid"}:
        note = (
            "Context7 is available to the configured provider when supported. "
            "Use it only for version-specific library/framework/SDK documentation that improves correctness."
        )
    else:
        note = "Context7 documentation was not injected for this task."
    return note, {key: value for key, value in bundle.items() if key != "bundle"}


def _structured_instruction(status_hint: str) -> str:
    return f"""
Return a short JSON object only. Do not wrap it in Markdown.
Required keys (use empty arrays when a section does not apply):
- status: one of planned, implemented, reviewed, approved, needs_changes, partial, blocked, no_changes_needed
- summary: concise operational summary
- files_modified: string array
- commands_run: string array
- tests_run: string array
- verification_needed: string array
- risks: string array
- follow_up: string array
- decisions: array of objects with string keys `key` and `value`; use [] when none
- constraints: string array
- assumptions: string array
- alternatives_rejected: string array
- acceptance_criteria: string array
- blockers: string array
- review_decision: approved, changes_required, inconclusive, or not_applicable; use not_applicable outside review
Prefer status `{status_hint}` when appropriate.
""".strip()


def _role_for_direct_task(
    *, role_name: str, provider: str | None, can_write: bool
) -> RoleConfig:
    cfg = load_config()
    default = RoleConfig(
        provider=cfg.router.default_provider,
        can_write=can_write,
        sandbox="workspace-write" if can_write else "read-only",
    )
    role = copy.deepcopy(cfg.roles.get(role_name, default))
    role.can_write = can_write
    role.sandbox = "workspace-write" if can_write else "read-only"
    if provider:
        role.profiles = []
        role.provider = provider
    elif not role.provider:
        role.provider = cfg.router.default_provider
    return role


def _first_execution_profile(role_name: str, role: RoleConfig) -> dict[str, Any]:
    cfg = load_config()
    profile = resolve_role_profiles(cfg, role_name, role)[0]
    return profile.to_dict()


def delegate_task_impl(
    *,
    workspace_root: str,
    task: str,
    acceptance_criteria: str = "",
    relevant_files: list[str] | None = None,
    extra_context: str = "",
    context7_libraries: list[str] | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    blocked = reentry_block_reason("delegate_task")
    if blocked:
        return blocked

    try:
        cwd = _workspace(workspace_root, access="write")
    except WorkspacePolicyError as exc:
        return exc.to_dict()

    role = _role_for_direct_task(
        role_name="implementer", provider=provider, can_write=True
    )
    profile = _first_execution_profile("implementer", role)
    docs_note, context7_meta = _context7_note(
        workspace_root=cwd,
        task_text="\n\n".join([task, acceptance_criteria, extra_context]),
        context7_libraries=context7_libraries,
    )
    files = (
        "\n".join(f"- {path}" for path in (relevant_files or [])) or "- Not provided"
    )
    prompt = f"""
You are the implementer role in a baldr-router controlled direct task.

Hard rules:
- Implement exactly the requested task with the smallest correct changes.
- Edit files only inside the workspace.
- Do not delegate to baldr-router or other agents.
- Do not use destructive commands.
- Run relevant tests/lint/typecheck/build commands when available and safe.
- Do not claim completion without reporting verification performed.

Task:
{task}

Acceptance criteria:
{acceptance_criteria or "Not provided"}

Relevant files:
{files}

Extra context:
{extra_context or "Not provided"}

Documentation context:
{docs_note}

{_structured_instruction("implemented")}
""".strip()

    run_id = new_run_id("task")
    env = child_provider_env(
        run_id=run_id,
        workflow=DIRECT_TASK_WORKFLOW,
        role="implementer",
        provider=str(profile["provider"]),
    )
    result = run_provider_role(
        provider=str(profile["provider"]),
        role_name="implementer",
        role=role,
        cwd=cwd,
        prompt=prompt,
        workflow=DIRECT_TASK_WORKFLOW,
        report_kind="implementation",
        extra_env=env,
        profile_name=str(profile["name"]),
        model=str(profile.get("model") or ""),
        reasoning_effort=str(profile.get("reasoning_effort") or ""),
        agent=str(profile.get("agent") or ""),
        effort=str(profile.get("effort") or ""),
        runner=str(profile.get("runner") or ""),
        session_scope=str(profile.get("session_scope") or ""),
    )
    result.setdefault("run_id", run_id)
    result["workspace_root"] = str(cwd)
    result["context7"] = context7_meta
    return result


def review_current_diff_impl(
    *,
    workspace_root: str,
    focus: str = "correctness, tests, regressions, security, and task compliance",
    extra_context: str = "",
    context7_libraries: list[str] | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    blocked = reentry_block_reason("review_current_diff")
    if blocked:
        return blocked

    try:
        cwd = _workspace(workspace_root, access="read")
    except WorkspacePolicyError as exc:
        return exc.to_dict()

    role = _role_for_direct_task(
        role_name="reviewer", provider=provider, can_write=False
    )
    profile = _first_execution_profile("reviewer", role)
    docs_note, context7_meta = _context7_note(
        workspace_root=cwd,
        task_text="\n\n".join([focus, extra_context]),
        context7_libraries=context7_libraries,
    )
    prompt = f"""
You are the reviewer role in a baldr-router controlled direct review.

Hard rules:
- Review the current git working tree diff.
- Do not modify files.
- Do not delegate to baldr-router or other agents.
- Focus on correctness, regression risk, tests, security, and the requested review focus.

Review focus:
{focus}

Extra context:
{extra_context or "Not provided"}

Documentation context:
{docs_note}

{_structured_instruction("reviewed")}
""".strip()

    run_id = new_run_id("review")
    env = child_provider_env(
        run_id=run_id,
        workflow=DIRECT_REVIEW_WORKFLOW,
        role="reviewer",
        provider=str(profile["provider"]),
    )
    result = run_provider_role(
        provider=str(profile["provider"]),
        role_name="reviewer",
        role=role,
        cwd=cwd,
        prompt=prompt,
        workflow=DIRECT_REVIEW_WORKFLOW,
        report_kind="review",
        extra_env=env,
        profile_name=str(profile["name"]),
        model=str(profile.get("model") or ""),
        reasoning_effort=str(profile.get("reasoning_effort") or ""),
        agent=str(profile.get("agent") or ""),
        effort=str(profile.get("effort") or ""),
        runner=str(profile.get("runner") or ""),
        session_scope=str(profile.get("session_scope") or ""),
    )
    result.setdefault("run_id", run_id)
    result["workspace_root"] = str(cwd)
    result["context7"] = context7_meta
    return result
