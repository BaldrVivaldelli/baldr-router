"""Immutable workflow configuration and participant identity resolution."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from baldr_router import __version__
from baldr_router.agent_api import AgentResolutionContext
from baldr_router.agent_gateway import external_agent_catalog_status, get_agent_gateway
from baldr_router.config import AppConfig, RoleConfig, WorkflowConfig
from baldr_router.execution_profiles import role_execution_plan
from baldr_router.team_resolution import resolve_team


def _resolved_snapshot(
    cfg: AppConfig,
    *,
    architect_provider: str | None,
    implementer_provider: str | None,
    reviewer_provider: str | None,
    max_rounds: int | None,
    role_profile_overrides: dict[str, list[str]] | None = None,
    workspace_mode: str | None = None,
    context7_policy: str | None = None,
    execution_preset: str | None = None,
    team_mode: str | None = None,
    agent_overrides: Mapping[str, str] | None = None,
    workspace_root: Path | None = None,
    catalog_loader: Callable[..., dict[str, Any]] = external_agent_catalog_status,
    gateway_factory: Callable[[], Any] = get_agent_gateway,
) -> dict[str, Any]:
    overrides = {
        "architect": architect_provider,
        "implementer": implementer_provider,
        "reviewer": reviewer_provider,
    }
    role_plans: dict[str, Any] = {}
    profile_overrides = role_profile_overrides or {}
    for role_name in ("architect", "implementer", "reviewer"):
        role = copy.deepcopy(cfg.roles[role_name])
        selected_profiles = profile_overrides.get(role_name)
        if selected_profiles:
            role.profiles = [
                str(item) for item in selected_profiles if str(item).strip()
            ]
        role_plans[role_name] = role_execution_plan(
            cfg, role_name, role, provider_override=overrides[role_name]
        )
        role_plans[role_name]["description"] = role.description

    selected_preset = str(execution_preset or "custom").strip().lower()
    if selected_preset not in {"fast", "balanced", "deep", "custom"}:
        raise ValueError(f"Unsupported execution preset: {selected_preset}")
    effort_by_preset = {"fast": "low", "balanced": "medium", "deep": "high"}
    if selected_preset == "fast":
        for plan in role_plans.values():
            plan["profiles"] = plan["profiles"][:1]
            plan["strategy"] = "first-success"
            plan["min_successes"] = 1
            plan["min_approvals"] = 1
    selected_effort = effort_by_preset.get(selected_preset)
    if selected_effort:
        for plan in role_plans.values():
            for profile in plan["profiles"]:
                profile["reasoning_effort"] = selected_effort
                profile["effort"] = selected_effort

    selected_team_mode = str(team_mode or "configured").strip().lower()
    selected_agent_overrides = dict(agent_overrides or {})
    catalog = (
        catalog_loader(workspace_root=workspace_root)
        if selected_team_mode in {"auto", "automatic"} or selected_agent_overrides
        else {"agents": []}
    )
    team_resolution = resolve_team(
        role_plans,
        catalog,
        mode=selected_team_mode,
        overrides=selected_agent_overrides,
    )
    role_plans = {role: dict(plan) for role, plan in team_resolution.plans.items()}

    gateway = None
    for role_name, plan in role_plans.items():
        requested_capabilities = (
            ("workspace.read", "workspace.write")
            if bool(plan.get("can_write"))
            else ("workspace.read",)
        )
        for profile in plan["profiles"]:
            agent_ref = str(profile.get("agent_ref") or "").strip()
            if not agent_ref:
                continue
            if gateway is None:
                gateway = gateway_factory()
            binding = gateway.binding(
                agent_ref,
                context=AgentResolutionContext(
                    workflow="architect-implement-review",
                    step_name=role_name,
                    requested_capabilities=requested_capabilities,
                ),
                expected_digest=str(profile.get("agent_manifest_digest") or ""),
            )
            profile.update(binding)
            if binding.get("provider"):
                profile["provider"] = binding["provider"]

    wf = cfg.workflows.get(cfg.router.default_workflow, WorkflowConfig())
    budget_values = {
        "max_parallel_participants": int(wf.max_parallel_participants),
        "max_participants_per_phase": int(wf.max_participants_per_phase),
        "max_total_participant_attempts": int(wf.max_total_participant_attempts),
    }
    budget_caps = {
        "max_parallel_participants": 32,
        "max_participants_per_phase": 64,
        "max_total_participant_attempts": 10_000,
    }
    for name, value in budget_values.items():
        if value < 1 or value > budget_caps[name]:
            raise ValueError(
                f"Workflow {name} must be between 1 and {budget_caps[name]}."
            )
    for role_name, plan in role_plans.items():
        profile_count = len(plan.get("profiles") or [])
        if profile_count > budget_values["max_participants_per_phase"]:
            raise ValueError(
                f"Role {role_name!r} resolves to {profile_count} participants; "
                f"the workflow budget allows "
                f"{budget_values['max_participants_per_phase']}."
            )
        if bool(plan.get("can_write")) and profile_count != 1:
            raise ValueError(
                f"Role {role_name!r} writes to the workspace and must resolve to "
                "exactly one participant."
            )
        requested_concurrency = max(1, int(plan.get("max_concurrency") or 1))
        plan["max_concurrency"] = (
            1
            if bool(plan.get("can_write"))
            else min(
                requested_concurrency,
                budget_values["max_parallel_participants"],
                max(1, profile_count),
            )
        )
    rounds = (
        max_rounds
        if max_rounds is not None
        else min(wf.max_rounds, cfg.safety.max_rounds)
    )
    if selected_preset == "fast":
        rounds = min(int(rounds), 1)
    elif selected_preset == "deep":
        rounds = min(cfg.safety.max_rounds, max(int(rounds), int(wf.max_rounds)))
    workspace_snapshot = asdict(cfg.workspace)
    selected_workspace_mode = str(workspace_mode or "").strip().lower()
    requested_safety_mode = selected_workspace_mode or None
    allow_non_git = selected_workspace_mode == "non-git"
    permission_gated_automatic = selected_workspace_mode in {
        "auto",
        "automatic",
    }
    workspace_snapshot.update(
        {
            "allow_non_git": allow_non_git,
            "effective_require_git_repository": bool(
                cfg.workspace.require_git_repository
                and not allow_non_git
                and not permission_gated_automatic
            ),
        }
    )
    if requested_safety_mode is not None:
        workspace_snapshot["requested_safety_mode"] = requested_safety_mode
    if selected_workspace_mode == "worktree":
        workspace_snapshot["write_isolation"] = "worktree"
        workspace_snapshot["dirty_workspace_policy"] = "reject"
        workspace_snapshot["publish_worktree_changes"] = True
    elif permission_gated_automatic:
        workspace_snapshot["write_isolation"] = "in-place"
        workspace_snapshot["dirty_workspace_policy"] = "in-place"
        workspace_snapshot["publish_worktree_changes"] = False
    elif selected_workspace_mode in {"current", "non-git"}:
        workspace_snapshot["write_isolation"] = "in-place"
        workspace_snapshot["dirty_workspace_policy"] = "in-place"
        workspace_snapshot["publish_worktree_changes"] = False
    context7_snapshot = asdict(cfg.context7)
    context7_snapshot["work_item_policy"] = (
        str(context7_policy or "auto").strip().lower()
    )
    if context7_snapshot["work_item_policy"] == "off":
        context7_snapshot["enabled"] = False
    elif context7_snapshot["work_item_policy"] == "on":
        context7_snapshot["enabled"] = True
        context7_snapshot["inject_docs"] = True
        if str(context7_snapshot.get("mode") or "off") == "off":
            context7_snapshot["mode"] = "hybrid"

    coordination_policy = {
        "contract": "baldr-orchestration-policy",
        "version": 1,
        "writer_policy": "exactly-one-per-write-phase",
        "budgets": {
            **budget_values,
            "max_rounds": max(0, min(int(rounds), cfg.safety.max_rounds)),
        },
        "roles": {
            role: {
                "strategy": str(plan.get("strategy") or "first-success"),
                "participant_count": len(plan.get("profiles") or []),
                "max_concurrency": int(plan.get("max_concurrency") or 1),
                "can_write": bool(plan.get("can_write")),
            }
            for role, plan in role_plans.items()
        },
    }

    return {
        "engine_version": __version__,
        "execution_preset": selected_preset,
        "team_resolution": team_resolution.to_dict(),
        "coordination": coordination_policy,
        "workflow": {**asdict(wf), **budget_values},
        "max_rounds": max(0, min(int(rounds), cfg.safety.max_rounds)),
        "role_plans": role_plans,
        "workspace": workspace_snapshot,
        "durability": asdict(cfg.durability),
        "sessions": asdict(cfg.sessions),
        "safety": asdict(cfg.safety),
        "context7": context7_snapshot,
    }


def _role_from_plan(plan: dict[str, Any]) -> RoleConfig:
    return RoleConfig(
        profiles=[],
        strategy=str(plan.get("strategy") or "first-success"),
        min_successes=int(plan.get("min_successes") or 1),
        resolution=str(plan.get("resolution") or ""),
        min_approvals=int(plan.get("min_approvals") or 1),
        max_concurrency=int(plan.get("max_concurrency") or 1),
        can_write=bool(plan.get("can_write")),
        sandbox=str(plan.get("sandbox") or "read-only"),
        description=str(plan.get("description") or ""),
    )


def _session_key(
    *,
    workspace_id: str,
    run_id: str,
    step_key: str,
    role: str,
    profile: dict[str, Any],
) -> str:
    scope = str(profile.get("session_scope") or "workflow")
    agent_identity = str(profile.get("agent_ref") or "")
    identity = ":".join(
        [
            str(profile.get("provider") or "provider"),
            role,
            agent_identity
            or str(profile.get("model") or profile.get("agent") or "default"),
            str(profile.get("name") or "profile"),
        ]
    )
    if scope == "global":
        return f"global:{identity}"
    if scope == "workspace":
        return f"workspace:{workspace_id}:{identity}"
    if scope == "task":
        return f"task:{run_id}:{step_key}:{identity}"
    return f"workflow:{run_id}:{identity}"


__all__ = ["_resolved_snapshot", "_role_from_plan", "_session_key"]
