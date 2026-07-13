"""Compatibility facade API backed by the versioned shared contract.

All clients keep the same three public intentions: ``setup``, ``status`` and
``run``. Rich clients such as the VS Code Baldr Console express item lifecycle
operations through typed arguments of those intentions rather than introducing
client-specific domain APIs.
"""

from __future__ import annotations

from typing import Any

from .facade_contract import (
    CONTRACT_VERSION as FACADE_CONTRACT_VERSION,
    INTENT_ORDER as FACADE_INTENTS,
    facade_contract,
    render_facade_prompt as _render_facade_prompt,
)
from .facade_runtime import run_facade, setup_facade, status_facade


def facade_intent(name: str) -> dict[str, Any]:
    contract = facade_contract()
    canonical = str(name).strip().lower().lstrip("/")
    try:
        raw = contract["intents"][canonical]
    except KeyError as exc:
        raise ValueError(
            f"Unknown facade intent: {name}. Expected: {', '.join(FACADE_INTENTS)}"
        ) from exc
    return {"name": canonical, **raw}


def render_facade_prompt(
    intent: str,
    *,
    workspace_root: str = "",
    task: str = "",
    client: str = "generic-mcp",
) -> str:
    prompt = _render_facade_prompt(
        intent,
        workspace_root=workspace_root or None,
        task=task or None,
    )
    return f"Client facade: {client}.\n\n{prompt}"


def facade_prompt(
    intent: str,
    *,
    workspace_root: str = "",
    task: str = "",
    client: str = "generic-mcp",
) -> str:
    return render_facade_prompt(
        intent,
        workspace_root=workspace_root,
        task=task,
        client=client,
    )


def facade_setup_plan(
    workspace_root: str | None = None,
    *,
    client: str = "generic-mcp",
    trust_current_workspace: bool = False,
    workspace_safety_mode: str | None = None,
    execution_preset: str | None = None,
    context7_policy: str | None = None,
    role_profiles: dict[str, list[str]] | None = None,
    allow_non_git: bool = False,
    profile_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = setup_facade(
        workspace_root,
        trust_current_workspace=trust_current_workspace,
        workspace_safety_mode=workspace_safety_mode,
        execution_preset=execution_preset,
        context7_policy=context7_policy,
        role_profiles=role_profiles,
        allow_non_git=allow_non_git,
        profile_definition=profile_definition,
    )
    result["client"] = client
    result.setdefault("context7_decision", result.get("context7_onboarding", {}))
    return result


def facade_status_report(
    workspace_root: str | None = None,
    *,
    client: str = "generic-mcp",
    recent_limit: int = 5,
    work_item_id: str | None = None,
    work_item_limit: int = 100,
    include_archived: bool = False,
    workbench_only: bool = False,
) -> dict[str, Any]:
    result = status_facade(
        workspace_root,
        run_limit=recent_limit,
        work_item_id=work_item_id,
        work_item_limit=work_item_limit,
        include_archived=include_archived,
        workbench_only=workbench_only,
    )
    result["client"] = client
    result.setdefault(
        "telemetry",
        {
            "recent": result.get("recent_runs", {}),
            "stats": result.get("health", {}).get("telemetry", {}).get("stats", {}),
        },
    )
    return result


def facade_run(
    workspace_root: str,
    task: str,
    *,
    client: str = "generic-mcp",
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
    work_item_action: str = "execute",
    work_item_id: str | None = None,
    title: str | None = None,
    workspace_mode: str | None = None,
    execution_preset: str | None = None,
    context7_policy: str | None = None,
    role_profiles: dict[str, list[str]] | None = None,
    remember_workspace: bool = False,
    allow_non_git: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    item_config: dict[str, Any] | None = None,
    phase_stage: str | None = None,
    phase_round: int | None = None,
    phase_run_ordinal: int | None = None,
    phase_cursor: str | None = None,
    phase_page_size: int = 20,
    deliverable_cursor: str | None = None,
    deliverable_page_size: int = 20,
) -> dict[str, Any]:
    result = run_facade(
        workspace_root=workspace_root,
        task=task,
        extra_context=extra_context,
        architect_provider=architect_provider,
        implementer_provider=implementer_provider,
        reviewer_provider=reviewer_provider,
        max_rounds=max_rounds,
        context7_libraries=context7_libraries,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        resume_run_id=resume_run_id,
        reconciliation_action=reconciliation_action,
        cancel=cancel,
        cancel_reason=cancel_reason,
        client_name=client,
        work_item_action=work_item_action,
        work_item_id=work_item_id,
        title=title,
        workspace_mode=workspace_mode,
        execution_preset=execution_preset,
        context7_policy=context7_policy,
        role_profiles=role_profiles,
        remember_workspace=remember_workspace,
        allow_non_git=allow_non_git,
        attachments=attachments,
        item_config=item_config,
        phase_stage=phase_stage,
        phase_round=phase_round,
        phase_run_ordinal=phase_run_ordinal,
        phase_cursor=phase_cursor,
        phase_page_size=phase_page_size,
        deliverable_cursor=deliverable_cursor,
        deliverable_page_size=deliverable_page_size,
    )
    metadata = result.setdefault("facade", {})
    metadata.update(
        {
            "intent": "run",
            "client": client,
            "contract_version": FACADE_CONTRACT_VERSION,
        }
    )
    return result
