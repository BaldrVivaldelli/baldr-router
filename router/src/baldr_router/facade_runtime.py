from __future__ import annotations

from typing import Any

from .config import load_config
from .context7_setup import context7_onboarding_plan
from .discovery.workspace_profile import workspace_profile
from .evidence import latest_evidence
from .facade_contract import facade_contract_status, get_facade_intent
from .phase_deliverables import (
    DELIVERABLE_INDEX_PAGE_CONTRACT,
    DELIVERABLE_INDEX_PAGE_VERSION,
    DELIVERABLE_PAGE_CONTRACT,
    DELIVERABLE_PAGE_VERSION,
    PhaseDeliverableError,
)
from .qualification import latest_qualification
from .status import doctor
from .telemetry import recent_runs
from .validation.lifecycle import ensure_quick_verification
from .work_items import WorkItemService, upsert_execution_profile, workbench_options
from .workflows import run_workflow_impl
from .workspace_policy import WorkspacePolicyError, inspect_workspace, trust_workspace


def setup_facade(
    workspace_root: str | None = None,
    *,
    trust_current_workspace: bool = False,
    workspace_safety_mode: str | None = None,
    execution_preset: str | None = None,
    context7_policy: str | None = None,
    role_profiles: dict[str, list[str]] | None = None,
    allow_non_git: bool = False,
    profile_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return or update the shared client-neutral setup state.

    This remains the only public setup intention. Client facades use its optional
    fields to implement small QuickPick/chip interactions rather than duplicating
    configuration logic.
    """

    configured_profile: dict[str, Any] | None = None
    if profile_definition:
        configured_profile = upsert_execution_profile(
            str(profile_definition.get("name") or ""),
            provider=str(profile_definition.get("provider") or ""),
            model=str(profile_definition.get("model") or ""),
            reasoning_effort=str(profile_definition.get("reasoning_effort") or ""),
            agent=str(profile_definition.get("agent") or ""),
            effort=str(profile_definition.get("effort") or ""),
            runner=str(profile_definition.get("runner") or ""),
            session_scope=str(profile_definition.get("session_scope") or ""),
            description=str(profile_definition.get("description") or ""),
        )

    workspace_trust: dict[str, Any] | None = None
    workbench_preferences: dict[str, Any] | None = None
    if workspace_root:
        service = WorkItemService()
        if any(
            value is not None
            for value in (
                workspace_safety_mode,
                execution_preset,
                context7_policy,
                role_profiles,
            )
        ):
            workbench_preferences = service.set_preferences(
                workspace_root,
                safety_mode=workspace_safety_mode,
                preset=execution_preset,
                context_mode=context7_policy,
                role_profiles=role_profiles,
                allow_non_git=allow_non_git,
            )
            workspace_trust = inspect_workspace(
                workspace_root,
                access="read",
                protected_non_git=str(
                    workbench_preferences.get("safety_mode") or ""
                ).lower()
                in {"auto", "automatic"},
            )
        elif trust_current_workspace:
            workspace_trust = trust_workspace(
                workspace_root,
                force=bool(allow_non_git and workspace_safety_mode == "non-git"),
                # Setup follows the default protected path when the caller did
                # not choose an explicit direct/non-Git mode. This trusts the
                # original folder without recording consent for unprotected
                # writes in ``trusted_non_git_roots``.
                protected_non_git=workspace_safety_mode in {None, "automatic", "auto"},
            )

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

    workbench: dict[str, Any] = {
        "configured_profile": configured_profile,
        "preferences": None,
        "profiles": None,
        "options": workbench_options(),
    }
    if workspace_root:
        service = WorkItemService()
        workbench["preferences"] = workbench_preferences or service.preferences(workspace_root)
        workbench["profiles"] = service.summary(workspace_root, limit=1).get("profiles")

    effective_safety_mode = str(
        ((workbench.get("preferences") or {}).get("safety_mode") or "")
    ).lower()
    return {
        # ``setup`` reports whether the requested configuration operation was
        # accepted. Provider health is exposed separately so a missing login
        # does not make harmless preference changes look like failed writes.
        "ok": True,
        "health_ok": bool(health.get("ok")),
        "intent": "setup",
        "contract": facade_contract_status(),
        "workspace_root": workspace_root,
        "workspace_trust": workspace_trust
        or (
            inspect_workspace(
                workspace_root,
                access="read",
                protected_non_git=effective_safety_mode in {"auto", "automatic"},
            )
            if workspace_root
            else None
        ),
        "health": health,
        "environment_probe": health.get("environment_probe"),
        "workspace_profile": profile or health.get("workspace_profile"),
        "verification": verification
        or health.get("verification")
        or latest_evidence(kind="lifecycle"),
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
        "workbench": workbench,
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
    work_item_id: str | None = None,
    work_item_limit: int = 100,
    include_archived: bool = False,
    workbench_only: bool = False,
) -> dict[str, Any]:
    """Return one compact client-neutral status document and console state."""

    service = WorkItemService()
    workbench = service.summary(
        workspace_root,
        limit=work_item_limit,
        selected_item_id=work_item_id,
        include_archived=include_archived,
        include_internal=not workbench_only,
    )
    if workbench_only:
        # The console refreshes while work is active. That hot path must not
        # rerun provider discovery, login checks, qualification, probes, or
        # lifecycle verification on every poll. The full status intent remains
        # the default for existing clients and explicit diagnostics.
        return {
            "ok": True,
            "intent": "status",
            "view": "workbench",
            "contract_version": facade_contract_status()["version"],
            "summary": {"work_item_counts": workbench.get("counts", {})},
            "workbench": workbench,
        }

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
    qualification_result = qualification.get("qualification") or {}
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
            "workspace_profile_available": bool(
                health.get("workspace_profile", {}).get("ok")
            ),
            "qualification_available": bool(qualification.get("available")),
            "qualification_accepted": qualification_result.get("status") == "qualified",
            "qualification_status": qualification_result.get("status"),
            "qualification_profile": qualification_result.get("profile"),
            "warnings": warnings,
            "work_item_counts": workbench.get("counts", {}),
        },
        "health": health,
        "environment_probe": health.get("environment_probe"),
        "workspace_profile": health.get("workspace_profile"),
        "verification": health.get("verification") or latest_evidence(kind="lifecycle"),
        "recent_runs": recent_runs(limit=max(1, min(run_limit, 20))),
        "qualification": qualification,
        "workbench": workbench,
    }


def _execution_config(
    *,
    architect_provider: str | None,
    implementer_provider: str | None,
    reviewer_provider: str | None,
    max_rounds: int | None,
    context7_libraries: list[str] | None,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "architect_provider": architect_provider,
            "implementer_provider": implementer_provider,
            "reviewer_provider": reviewer_provider,
            "max_rounds": max_rounds,
            "context7_libraries": context7_libraries,
        }.items()
        if value is not None
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
    """Execute or manage a durable item through the frozen ``run`` intention."""

    action = str(work_item_action or "execute").strip().lower().replace("_", "-")
    aliases = {
        "draft": "create-item",
        "create": "create-item",
        "create-item": "create-item",
        "update": "update-item",
        "update-item": "update-item",
        "continue": "continue-item",
        "continue-item": "continue-item",
        "start": "start-item",
        "start-item": "start-item",
        "cancel": "cancel-item",
        "cancel-item": "cancel-item",
        "reconcile": "reconcile-item",
        "reconcile-item": "reconcile-item",
        "archive": "archive-item",
        "archive-item": "archive-item",
        "restore": "restore-item",
        "restore-item": "restore-item",
        "delete": "delete-item",
        "delete-item": "delete-item",
        "inspect-phase": "inspect-item-phase",
        "inspect-item-phase": "inspect-item-phase",
        "list-deliverables": "list-item-deliverables",
        "list-item-deliverables": "list-item-deliverables",
        "execute": "execute",
    }
    action = aliases.get(action, action)
    service = WorkItemService()

    try:
        if remember_workspace:
            service.set_preferences(
                workspace_root,
                safety_mode=workspace_mode,
                preset=execution_preset,
                context_mode=context7_policy,
                role_profiles=role_profiles,
                allow_non_git=allow_non_git,
            )

        execution = _execution_config(
            architect_provider=architect_provider,
            implementer_provider=implementer_provider,
            reviewer_provider=reviewer_provider,
            max_rounds=max_rounds,
            context7_libraries=context7_libraries,
        )
        execution.update(item_config or {})

        if action == "create-item":
            item = service.create(
                workspace_root=workspace_root,
                task=task,
                title=title,
                extra_context=extra_context,
                attachments=attachments,
                safety_mode=workspace_mode,
                preset=execution_preset,
                context_mode=context7_policy,
                role_profiles=role_profiles,
                config=execution,
                allow_non_git=allow_non_git,
                source=client_name,
            )
            return {"ok": True, "intent": "run", "operation": action, "work_item": item}

        if action == "update-item":
            if not work_item_id:
                raise ValueError("work_item_id is required for update-item.")
            item = service.update(
                work_item_id,
                title=title,
                task=task or None,
                extra_context=extra_context if extra_context else None,
                attachments=attachments,
                safety_mode=workspace_mode,
                preset=execution_preset,
                context_mode=context7_policy,
                role_profiles=role_profiles,
                config=execution,
                allow_non_git=allow_non_git,
            )
            return {"ok": True, "intent": "run", "operation": action, "work_item": item}

        if action == "continue-item":
            if not work_item_id:
                raise ValueError("work_item_id is required for continue-item.")
            item = service.continue_item(
                work_item_id,
                workspace_root=workspace_root,
                request=task,
                extra_context=extra_context,
                attachments=attachments,
                source=client_name,
            )
            work_item_id = str(item["id"])
            result = service.start(
                work_item_id,
                client_name=client_name,
                dry_run=dry_run,
                context7_libraries=context7_libraries,
            )
            result.setdefault("intent", "run")
            result["operation"] = action
            return result

        if action == "archive-item":
            if not work_item_id:
                raise ValueError("work_item_id is required for archive-item.")
            return {
                "ok": True,
                "intent": "run",
                "operation": action,
                "work_item": service.archive(work_item_id),
            }

        if action == "restore-item":
            if not work_item_id:
                raise ValueError("work_item_id is required for restore-item.")
            return {
                "ok": True,
                "intent": "run",
                "operation": action,
                "work_item": service.restore(work_item_id),
            }

        if action == "delete-item":
            if not work_item_id:
                raise ValueError("work_item_id is required for delete-item.")
            return {
                "ok": True,
                "intent": "run",
                "operation": action,
                "deleted_work_item": service.delete(work_item_id),
            }

        if action == "inspect-item-phase":
            if not work_item_id or phase_stage is None or phase_round is None:
                raise ValueError(
                    "work_item_id, phase_stage, and phase_round are required for inspect-item-phase."
                )
            page = service.inspect_phase(
                work_item_id,
                workspace_root=workspace_root,
                stage=phase_stage,
                round_number=phase_round,
                run_ordinal=phase_run_ordinal,
                cursor=phase_cursor,
                page_size=phase_page_size,
            )
            return {
                "ok": True,
                "intent": "run",
                "operation": action,
                "contract": DELIVERABLE_PAGE_CONTRACT,
                "version": DELIVERABLE_PAGE_VERSION,
                **page,
            }

        if action == "list-item-deliverables":
            if not work_item_id:
                raise ValueError(
                    "work_item_id is required for list-item-deliverables."
                )
            page = service.list_deliverables(
                work_item_id,
                workspace_root=workspace_root,
                cursor=deliverable_cursor,
                page_size=deliverable_page_size,
            )
            return {
                "ok": True,
                "intent": "run",
                "operation": action,
                "contract": DELIVERABLE_INDEX_PAGE_CONTRACT,
                "version": DELIVERABLE_INDEX_PAGE_VERSION,
                **page,
            }

        if action == "cancel-item" or (cancel and work_item_id):
            if not work_item_id:
                raise ValueError("work_item_id is required for cancel-item.")
            result = service.cancel(
                work_item_id,
                reason=cancel_reason,
                client_name=client_name,
            )
            result.setdefault("intent", "run")
            result["operation"] = "cancel-item"
            return result

        if action == "reconcile-item":
            if not work_item_id or not reconciliation_action:
                raise ValueError(
                    "work_item_id and reconciliation_action are required for reconcile-item."
                )
            result = service.reconcile(
                work_item_id,
                action=reconciliation_action,
                client_name=client_name,
            )
            result.setdefault("intent", "run")
            result["operation"] = action
            return result

        # Backwards-compatible durable-run controls remain available when no
        # WorkItem ID exists yet.
        if (resume_run_id or cancel) and not work_item_id:
            result = run_workflow_impl(
                workspace_root=workspace_root,
                task=task,
                extra_context=extra_context,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
                resume_run_id=resume_run_id,
                reconciliation_action=reconciliation_action,
                cancel=cancel,
                cancel_reason=cancel_reason,
                client_name=client_name,
                workspace_mode=workspace_mode,
                context7_policy=context7_policy,
                role_profile_overrides=role_profiles,
                execution_preset=execution_preset,
            )
            result.setdefault("intent", "run")
            result["operation"] = "execute"
            return result

        if not work_item_id:
            if not task.strip():
                raise ValueError("task must not be empty.")
            item = service.create(
                workspace_root=workspace_root,
                task=task,
                title=title,
                extra_context=extra_context,
                attachments=attachments,
                safety_mode=workspace_mode,
                preset=execution_preset,
                context_mode=context7_policy,
                role_profiles=role_profiles,
                config=execution,
                allow_non_git=allow_non_git,
                source=client_name,
            )
            work_item_id = str(item["id"])
        elif execution or any(
            value is not None
            for value in (workspace_mode, execution_preset, context7_policy, role_profiles)
        ):
            service.update(
                work_item_id,
                safety_mode=workspace_mode,
                preset=execution_preset,
                context_mode=context7_policy,
                role_profiles=role_profiles,
                config=execution,
                allow_non_git=allow_non_git,
            )

        result = service.start(
            work_item_id,
            client_name=client_name,
            dry_run=dry_run,
            context7_libraries=context7_libraries,
        )
        result.setdefault("intent", "run")
        result["operation"] = "start-item" if action == "start-item" else "execute"
        return result
    except WorkspacePolicyError as exc:
        result = exc.to_dict()
        result.update({"intent": "run", "operation": action})
        # ``execute`` may already have materialized a durable draft before the
        # workspace policy blocks provider access. Return that item so rich
        # facades can select it, guide the user, and retry without losing text.
        if work_item_id:
            try:
                result["work_item"] = service.get(work_item_id)
            except KeyError:
                pass
        return result
    except PhaseDeliverableError as exc:
        return {
            "ok": False,
            "intent": "run",
            "operation": action,
            "error": {"code": exc.code, "message": str(exc)},
            "reason": str(exc),
        }
    except (KeyError, ValueError) as exc:
        return {
            "ok": False,
            "intent": "run",
            "operation": action,
            "error": {"code": "work_item_invalid_request", "message": str(exc)},
            "reason": str(exc),
        }


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
    intent = get_facade_intent(intent_id)
    if intent.id == "setup":
        return setup_facade(workspace_root)
    if intent.id == "status":
        return status_facade(workspace_root, run_limit=recent_limit, work_item_id=work_item_id)
    if intent.id == "run":
        if not workspace_root:
            return {"ok": False, "intent": "run", "reason": "workspace_root is required"}
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
    raise AssertionError(f"Unhandled facade intent: {intent.id}")
