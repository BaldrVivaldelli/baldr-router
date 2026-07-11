"""Compatibility facade API backed by the versioned shared contract.

New code should import from :mod:`baldr_router.facade_contract` and
:mod:`baldr_router.facade_runtime`. This module keeps the v0.16 Python API
compact for adapters and tests without duplicating domain logic.
"""

from __future__ import annotations

from typing import Any

from .facade_contract import (
    CONTRACT_VERSION as FACADE_CONTRACT_VERSION,
    INTENT_ORDER as FACADE_INTENTS,
    facade_contract,
    render_facade_prompt as _render_facade_prompt,
)
from .facade_runtime import (
    run_facade,
    setup_facade,
    status_facade,
)


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
) -> dict[str, Any]:
    result = setup_facade(
        workspace_root, trust_current_workspace=trust_current_workspace
    )
    result["client"] = client
    result.setdefault("context7_decision", result.get("context7_onboarding", {}))
    return result


def facade_status_report(
    workspace_root: str | None = None,
    *,
    client: str = "generic-mcp",
    recent_limit: int = 5,
) -> dict[str, Any]:
    result = status_facade(workspace_root, run_limit=recent_limit)
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
