from __future__ import annotations

from typing import Any

from .context7_setup import context7_onboarding_plan
from .facade_contract import facade_contract_status, get_facade_intent
from .status import doctor
from .config import load_config
from .discovery.workspace_profile import workspace_profile
from .evidence import latest_evidence
from .validation.lifecycle import ensure_quick_verification
from .qualification import latest_qualification
from .telemetry import recent_runs
from .workflows import run_workflow_impl
from .workspace_policy import inspect_workspace, trust_workspace


def setup_facade(
    workspace_root: str | None = None, *, trust_current_workspace: bool = False
) -> dict[str, Any]:
    """Return a client-neutral setup plan without collecting secrets."""
    workspace_trust: dict[str, Any] | None = None
    if workspace_root and trust_current_workspace:
        workspace_trust = trust_workspace(workspace_root)
    cfg = load_config()
    verification: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    if workspace_root and trust_current_workspace:
        if cfg.probe.enabled:
            profile = workspace_profile(workspace_root)
        if cfg.verification.enabled and cfg.verification.run_on_setup:
            verification = ensure_quick_verification(
                workspace_root=workspace_root,
                client_id=__import__("os").environ.get("BALDR_CLIENT_ID") or "facade",
            )
    health = doctor(workspace_root)
    context7 = context7_onboarding_plan()
    router = health.get("router", {})
    codex = health.get("codex", {})

    actions: list[dict[str, Any]] = []
    if not codex.get("found"):
        actions.append(
            {
                "id": "install-codex",
                "blocking": True,
                "message": "Install Codex CLI in the environment where Baldr Router runs.",
            }
        )
    login = codex.get("login_status")
    if codex.get("found") and isinstance(login, dict) and not login.get("ok"):
        actions.append(
            {
                "id": "login-codex",
                "blocking": True,
                "message": "Run `codex login` and choose ChatGPT sign-in.",
            }
        )

    return {
        "ok": bool(health.get("ok")),
        "intent": "setup",
        "contract": facade_contract_status(),
        "workspace_root": workspace_root,
        "workspace_trust": workspace_trust
        or (inspect_workspace(workspace_root, access="read") if workspace_root else None),
        "health": health,
        "environment_probe": health.get("environment_probe"),
        "workspace_profile": profile or health.get("workspace_profile"),
        "verification": verification or health.get("verification") or latest_evidence(kind="lifecycle"),
        "questions": [
            {
                "id": "roles",
                "optional": True,
                "question": "Keep the current execution profiles or customize architecture, implementation, and review?",
                "default": "keep-current",
                "current": router.get("roles", {}),
            },
            {
                "id": "context7",
                "optional": True,
                "question": "Enable optional Context7 documentation enrichment?",
                "choices": [
                    "not-now",
                    "secure-client-secret-storage",
                    "existing-environment-variable",
                    "instructions-only",
                ],
            },
        ],
        "context7_onboarding": context7,
        "actions": actions,
        "secret_policy": {
            "ask_in_chat": False,
            "accepted_sources": [
                "client secret storage",
                "approved environment variable",
                "router local secret store",
            ],
        },
    }


def status_facade(
    workspace_root: str | None = None,
    *,
    run_limit: int = 5,
) -> dict[str, Any]:
    """Return one compact, client-neutral status document."""
    health = doctor(workspace_root)
    context7 = health.get("context7", {})
    warnings: list[str] = []

    if not health.get("ok") and health.get("next_step"):
        warnings.append(str(health["next_step"]))
    if context7.get("enabled") and not context7.get("api_key_available"):
        warnings.append(
            "Context7 is enabled but its API key is unavailable to this router process."
        )

    qualification = latest_qualification()
    latest_qualification_result = qualification.get("qualification") or {}
    return {
        "ok": bool(health.get("ok")),
        "intent": "status",
        "contract_version": facade_contract_status()["version"],
        "summary": {
            "default_provider": health.get("router", {}).get("default_provider"),
            "default_workflow": health.get("router", {}).get("default_workflow"),
            "codex_found": health.get("codex", {}).get("found"),
            "codex_runner": health.get("codex", {}).get("runner"),
            "context7_enabled": context7.get("enabled"),
            "context7_mode": context7.get("mode"),
            "verification_ok": (health.get("verification") or {}).get("ok"),
            "workspace_profile_available": bool(health.get("workspace_profile", {}).get("ok")),
            "qualification_available": bool(qualification.get("available")),
            "qualification_accepted": latest_qualification_result.get("status") == "qualified",
            "qualification_status": latest_qualification_result.get("status"),
            "qualification_profile": latest_qualification_result.get("profile"),
            "warnings": warnings,
        },
        "health": health,
        "environment_probe": health.get("environment_probe"),
        "workspace_profile": health.get("workspace_profile"),
        "verification": health.get("verification") or latest_evidence(kind="lifecycle"),
        "recent_runs": recent_runs(limit=max(1, min(run_limit, 20))),
        "qualification": qualification,
    }


def run_facade(
    *,
    workspace_root: str,
    task: str,
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
) -> dict[str, Any]:
    """Execute the frozen workflow through the shared facade path."""
    if not task.strip() and not (cancel or resume_run_id):
        return {"ok": False, "intent": "run", "reason": "task must not be empty"}

    result = run_workflow_impl(
        workspace_root=workspace_root,
        task=task,
        workflow="architect-implement-review",
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
        client_name=client_name,
    )
    result.setdefault("intent", "run")
    return result


def execute_facade_intent(
    intent_id: str,
    *,
    workspace_root: str | None = None,
    task: str | None = None,
    extra_context: str = "",
    architect_provider: str | None = None,
    implementer_provider: str | None = None,
    reviewer_provider: str | None = None,
    max_rounds: int | None = None,
    context7_libraries: list[str] | None = None,
    dry_run: bool = False,
    recent_limit: int = 5,
    idempotency_key: str | None = None,
    resume_run_id: str | None = None,
    reconciliation_action: str | None = None,
    cancel: bool = False,
    cancel_reason: str = "Cancellation requested by client.",
    client_name: str = "generic-mcp",
) -> dict[str, Any]:
    intent = get_facade_intent(intent_id)
    if intent.id == "setup":
        return setup_facade(workspace_root)
    if intent.id == "status":
        return status_facade(workspace_root, run_limit=recent_limit)
    if intent.id == "run":
        if not workspace_root:
            return {
                "ok": False,
                "intent": "run",
                "reason": "workspace_root is required",
            }
        return run_facade(
            workspace_root=workspace_root,
            task=task or "",
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
            client_name=client_name,
        )
    raise AssertionError(f"Unhandled facade intent: {intent.id}")
