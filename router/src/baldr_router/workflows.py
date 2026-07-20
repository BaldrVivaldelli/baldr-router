from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from .config import RoleConfig, load_config, save_config
from .context7 import prepare_context7_bundle
from .durability.engine import (
    DurableWorkflowEngine,
    _has_blockers as _durable_has_blockers,
    _resolved_snapshot,
)
from .agent_api import (
    AgentContractError,
    AgentDigestMismatchError,
    AgentNotFoundError,
    AgentTransportError,
)
from .agent_gateway import (
    AgentPolicyError,
    configured_agent_bindings_status,
    external_agent_catalog_status,
)
from .durability.recovery import recover_stale_runs
from .durability.store import DurableStore
from .execution_profiles import role_execution_plan
from .provider_registry import provider_status, run_provider_role
from .runtime_guard import reentry_block_reason
from .team_resolution import TeamResolutionError
from .workspace_policy import WorkspacePolicyError, require_workspace

WORKFLOW_ARCHITECT_IMPLEMENT_REVIEW = "architect-implement-review"


def _has_blockers(review_result: dict[str, Any]) -> bool:
    """Backwards-compatible helper delegated to the durable engine."""
    return _durable_has_blockers(review_result)


def _role_with_override(base: RoleConfig, provider_override: str | None) -> RoleConfig:
    role = copy.deepcopy(base)
    if provider_override:
        # An explicit one-off provider override intentionally bypasses named
        # profiles while retaining the role's permissions.
        role.profiles = []
        role.provider = provider_override
    return role


def list_roles() -> dict[str, Any]:
    cfg = load_config()
    plans: dict[str, Any] = {}
    for name, role in cfg.roles.items():
        try:
            plans[name] = role_execution_plan(cfg, name, role)
        except Exception as exc:
            plans[name] = {"ok": False, "reason": str(exc)}
    return {
        "ok": True,
        "roles": {name: asdict(role) for name, role in cfg.roles.items()},
        "execution_profiles": {
            name: asdict(profile) for name, profile in cfg.execution_profiles.items()
        },
        "resolved": plans,
    }


def list_workflows() -> dict[str, Any]:
    cfg = load_config()
    return {
        "ok": True,
        "default_workflow": cfg.router.default_workflow,
        "workflows": {
            name: asdict(workflow) for name, workflow in cfg.workflows.items()
        },
    }


def set_role_provider(
    role: str,
    provider: str,
    *,
    agent: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    current = cfg.roles.get(role, RoleConfig())
    # This command is the simple inline-profile path. Reusable n/m/l phase
    # profiles remain configurable under [execution_profiles] + roles.*.profiles.
    current.profiles = []
    current.provider = provider
    if agent is not None:
        current.agent = agent
    if effort is not None:
        current.effort = effort
    cfg.roles[role] = current
    saved = save_config(cfg)
    return {
        "ok": True,
        "config_path": str(saved),
        "role": role,
        "config": asdict(current),
        "note": (
            "The role now uses one inline execution profile. To configure multiple "
            "profiles for this phase, set roles.<name>.profiles in config.toml."
        ),
    }


def run_workflow_impl(
    *,
    workspace_root: str,
    task: str,
    workflow: str | None = None,
    extra_context: str = "",
    architect_provider: str | None = None,
    implementer_provider: str | None = None,
    reviewer_provider: str | None = None,
    max_rounds: int | None = None,
    context7_libraries: list[str] | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    resume_run_id: str | None = None,
    reconciliation_action: str | None = None,
    cancel: bool = False,
    cancel_reason: str = "Cancellation requested by client.",
    client_name: str = "generic-mcp",
    workspace_mode: str | None = None,
    context7_policy: str | None = None,
    role_profile_overrides: dict[str, list[str]] | None = None,
    execution_preset: str | None = None,
    work_item_id: str | None = None,
    team_mode: str | None = None,
    agent_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    if cfg.safety.prevent_router_reentry:
        blocked = reentry_block_reason("run_workflow")
        if blocked:
            return blocked

    workflow_name = workflow or cfg.router.default_workflow
    if workflow_name != WORKFLOW_ARCHITECT_IMPLEMENT_REVIEW:
        return {
            "ok": False,
            "reason": f"Workflow {workflow_name!r} is not implemented.",
            "implemented_workflows": [WORKFLOW_ARCHITECT_IMPLEMENT_REVIEW],
        }

    store = DurableStore()
    engine = DurableWorkflowEngine(store=store, provider_runner=run_provider_role)
    if cancel:
        if not resume_run_id:
            return {
                "ok": False,
                "status": "invalid_request",
                "error": {"code": "cancel_requires_run_id"},
                "reason": "Cancellation requires resume_run_id.",
            }
        return engine.request_cancel(resume_run_id, reason=cancel_reason)

    # Direct work in the already trusted workspace is the product default used
    # by the durable work-item path and every first-party facade.  Keep
    # ``automatic`` only as an explicit opt-in for clients that want a
    # per-task write authorization pause.
    selected_workspace_mode = str(workspace_mode or "current").strip().lower()
    protected_non_git = selected_workspace_mode in {"auto", "automatic"}
    try:
        cwd = require_workspace(
            workspace_root,
            access="read" if dry_run else "write",
            protected_non_git=protected_non_git,
        )
    except WorkspacePolicyError as exc:
        return exc.to_dict()

    try:
        snapshot = _resolved_snapshot(
            cfg,
            architect_provider=architect_provider,
            implementer_provider=implementer_provider,
            reviewer_provider=reviewer_provider,
            max_rounds=max_rounds,
            role_profile_overrides=role_profile_overrides,
            workspace_mode=selected_workspace_mode,
            context7_policy=context7_policy,
            execution_preset=execution_preset,
            team_mode=team_mode,
            agent_overrides=agent_overrides,
            workspace_root=cwd,
        )
    except TeamResolutionError as exc:
        return {
            "ok": False,
            "status": "invalid_team_resolution",
            "error": {"code": exc.code, "retryable": False, "role": exc.role},
            "reason": str(exc),
        }
    except AgentDigestMismatchError as exc:
        return {
            "ok": False,
            "status": "invalid_agent_binding",
            "error": {"code": "agent_manifest_digest_mismatch", "retryable": False},
            "reason": str(exc),
        }
    except AgentNotFoundError as exc:
        return {
            "ok": False,
            "status": "invalid_agent_binding",
            "error": {"code": "agent_not_found", "retryable": True},
            "reason": str(exc),
        }
    except AgentContractError as exc:
        return {
            "ok": False,
            "status": "invalid_agent_binding",
            "error": {"code": "agent_contract_invalid", "retryable": False},
            "reason": str(exc),
        }
    except AgentPolicyError as exc:
        return {
            "ok": False,
            "status": "invalid_agent_binding",
            "error": {"code": "agent_policy_denied", "retryable": False},
            "reason": str(exc),
        }
    except AgentTransportError as exc:
        return {
            "ok": False,
            "status": "agent_transport_unavailable",
            "error": {
                "code": "agent_transport_failed",
                "retryable": exc.retryable,
            },
            "reason": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"Invalid execution profile configuration: {exc}",
        }

    if dry_run:
        bundle = prepare_context7_bundle(
            workspace_root=cwd,
            task_text=task + "\n" + extra_context,
            libraries=context7_libraries,
            config_override=snapshot.get("context7", {}),
        )
        meta = {key: value for key, value in bundle.items() if key != "bundle"}
        return engine.dry_run(
            workspace_root=cwd,
            task=task,
            snapshot=snapshot,
            context7_meta=meta,
        )

    return engine.run(
        workspace_root=cwd,
        task=task,
        extra_context=extra_context,
        config_snapshot=snapshot,
        context7_libraries=context7_libraries,
        client_name=client_name,
        idempotency_key=idempotency_key,
        resume_run_id=resume_run_id,
        reconciliation_action=reconciliation_action,
        cancel=cancel,
        cancel_reason=cancel_reason,
        work_item_id=work_item_id,
    )


def workflow_status() -> dict[str, Any]:
    cfg = load_config()
    store = DurableStore()
    recovery = (
        recover_stale_runs(store)
        if cfg.durability.enabled and cfg.durability.recovery_on_start
        else {"ok": True, "count": 0, "runs": []}
    )
    roles = list_roles()
    agents = {
        **external_agent_catalog_status(),
        "configured_bindings": configured_agent_bindings_status(roles["resolved"]),
    }
    return {
        "ok": bool(agents["ok"] and agents["configured_bindings"]["ok"]),
        "default_workflow": cfg.router.default_workflow,
        "safety": asdict(cfg.safety),
        "durability": {
            **asdict(cfg.durability),
            "database_path": str(store.path),
            "schema": store.schema_status(),
            "nonterminal_runs": store.list_nonterminal_runs(),
            "recovery": recovery,
            "maintenance": store.maintenance(full=False),
        },
        "sessions": asdict(cfg.sessions),
        "providers": provider_status(),
        "agents": agents,
        "roles": roles["roles"],
        "execution_profiles": roles["execution_profiles"],
        "resolved_role_plans": roles["resolved"],
        "workflows": {
            name: asdict(workflow) for name, workflow in cfg.workflows.items()
        },
    }
