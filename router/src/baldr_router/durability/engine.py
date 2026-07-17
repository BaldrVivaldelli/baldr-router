from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import socket
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from baldr_router import __version__
from baldr_router.config import AppConfig, RoleConfig, WorkflowConfig
from baldr_router.context7 import prepare_context7_bundle
from baldr_router.execution_profiles import role_execution_plan
from baldr_router.phase_deliverables import materialize_phase_deliverable
from baldr_router.process_control import terminate_processes_for_run
from baldr_router.provider_registry import (
    provider_isolation_status,
    provider_runtime_identity,
    run_provider_role,
)
from baldr_router.provider_activity import (
    emit_provider_activity,
    generic_activity_for_role,
)
from baldr_router.redaction import redact_text
from baldr_router.runtime_guard import child_provider_env, new_run_id
from baldr_router.telemetry import append_run, utc_now_iso

from .evidence import create_workflow_evidence
from .git_workspace import GitWorkspaceError, GitWorkspaceManager, WorkspaceExecution
from .heartbeat import LeaseHeartbeat, WorkflowCancelled
from .identity import identities_match, request_fingerprint, workspace_identity
from .recovery import recover_stale_runs
from .reducers import reduce_phase
from .store import (
    DurableStore,
    IdempotencyConflict,
    LeaseFenceError,
    LeaseToken,
)

ProviderRunner = Callable[..., dict[str, Any]]
FaultHook = Callable[[str, dict[str, Any]], None]

TERMINAL_RUN_STATES = {"approved", "needs_changes", "blocked", "failed", "cancelled"}
RECONCILIATION_ACTIONS = {
    "authorize_changes",
    "decline_changes",
    "resume_from_checkpoint",
    "accept_existing_changes",
    "discard_worktree",
    "inspect_shadow",
    "continue_from_shadow",
    "apply_shadow_changes",
    "discard_shadow",
    "mark_failed",
}


class SimulatedProcessCrash(BaseException):
    """Test-only process-loss signal that deliberately leaves leases stale."""


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _safe_text(value: Any, limit: int = 6000) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "…"


def _extract_summary(result: dict[str, Any]) -> str:
    report = result.get("final_report")
    if isinstance(report, dict):
        return _safe_text(report, 5000)
    for key in ("final_text", "stdout", "stdout_tail", "stderr"):
        if result.get(key):
            return _safe_text(result[key], 5000)
    return _safe_text(result, 5000)


def _reported_file_changes(result: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return the provider's bounded path claims for workspace verification."""

    report = result.get("final_report")
    if not isinstance(report, Mapping):
        return []
    fields = (
        ("files_added", "added"),
        ("files_modified", "modified"),
        ("files_deleted", "deleted"),
    )
    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for field, kind in fields:
        values = report.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str) or not value.strip() or value in seen:
                continue
            seen.add(value)
            changes.append({"path": value, "kind": kind})
            if len(changes) >= 100:
                return changes
    return changes


def _write_authorization_request(result: dict[str, Any]) -> str | None:
    report = result.get("final_report")
    if not isinstance(report, dict):
        return "Crear o modificar archivos para completar el pedido."
    decisions = report.get("decisions")
    if not isinstance(decisions, dict):
        return "Crear o modificar archivos para completar el pedido."
    authorization = str(decisions.get("write_authorization") or "").strip().lower()
    if authorization == "not_required":
        return None
    request = str(decisions.get("write_request") or "").strip()
    return request or "Crear o modificar archivos para completar el pedido."


def _requires_write_authorization(config_snapshot: dict[str, Any]) -> bool:
    workspace = config_snapshot.get("workspace") or {}
    requested_value = workspace.get("requested_safety_mode")
    if requested_value is None:
        # Runs created before this mode must keep their original isolated or
        # direct semantics when their immutable snapshot is resumed.
        return False
    requested = str(requested_value).strip().lower()
    return requested in {"", "auto", "automatic"}


def _has_blockers(result: dict[str, Any]) -> bool:
    report = result.get("final_report")
    if isinstance(report, dict):
        status = str(report.get("status", "")).lower()
        if status in {"blocked", "needs_changes", "partial"}:
            return True
        text = "\n".join(
            str(item)
            for item in list(report.get("risks") or [])
            + list(report.get("verification_needed") or [])
        ).lower()
        return any(
            word in text
            for word in ("blocker:", "[blocker]", "blocking:", "must fix:", "critical:")
        )
    if isinstance(result.get("participants"), list):
        return any(_has_blockers(item) for item in result["participants"])
    return not bool(result.get("ok"))


def _structured_instruction(status_hint: str) -> str:
    return f"""
Return a short JSON object only. Do not wrap it in Markdown.
Required keys (use empty arrays when a section does not apply):
- status: one of planned, implemented, reviewed, approved, needs_changes, partial, blocked, no_changes_needed
- summary: concise operational summary
- interpretation: one sentence explaining what you understood the person needs
- scope: string array describing what is and is not included
- approach: string array describing the chosen approach as conclusions, not hidden reasoning
- plan_steps: ordered string array of concrete planned steps
- work_completed: string array of concrete work already completed
- work_next: string array of concrete work still remaining
- findings: string array of review findings; use [] when none
- corrections: string array of corrections applied; use [] when none
- verification_evidence: string array of observable checks and their outcomes; do not claim a pass without evidence
- changes_added: concise user-facing descriptions of capabilities or content introduced; use [] when none
- changes_modified: concise user-facing descriptions of existing behavior or content adjusted; use [] when none
- changes_removed: concise user-facing descriptions of behavior or content removed; use [] when none
- files_added: paths of files actually created; use [] when none
- files_modified: paths of existing files actually changed; use [] when none
- files_deleted: paths of files actually removed; use [] when none
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
Write `summary`, `interpretation`, and every user-facing list item in the same language as the user's task.
Use concise, plain language that a non-technical reader can understand;
keep necessary technical identifiers only in their
dedicated fields. Report conclusions and observable evidence only. Never include hidden
reasoning, private chain-of-thought, or an analysis transcript.
""".strip()


def _runner_accepts_activity_sink(runner: ProviderRunner) -> bool:
    """Preserve compatibility with injected provider runners from older clients."""

    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "activity_sink"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def architect_prompt(task: str, extra_context: str, context7_note: str) -> str:
    return f"""
You are an architecture participant in a Baldr-controlled durable workflow.

Hard rules:
- Planning starts without permission to change files. When the requested outcome
  needs file creation, editing, deletion, or commands with workspace side effects,
  request the person's authorization instead of treating that need as a failure.
- Do not delegate to Baldr or other agents.
- Produce a concise implementation plan.
- Identify risks, likely files, tests, and acceptance criteria.
- In `decisions`, always include `write_authorization`: use `required` when the
  plan needs workspace changes and `not_required` when the result is read-only.
- When authorization is required, also include `write_request` with one concise,
  user-facing sentence describing the changes that will be allowed.
- A pending authorization is not a blocker. Return status `planned` with an empty
  `blockers` array unless an external condition prevents the plan from proceeding.

Task:
{task}

Extra context:
{extra_context or "Not provided"}

{context7_note}

{_structured_instruction("planned")}
""".strip()


def implementer_prompt(
    task: str, plan_summary: str, extra_context: str, context7_note: str
) -> str:
    return f"""
You are an implementation participant in a Baldr-controlled durable workflow.

Hard rules:
- Implement the architecture artifact below with the smallest correct changes.
- Modify files only inside the supplied workspace.
- Do not delegate to Baldr or other agents.
- Do not use destructive commands.
- Run relevant tests/lint/typecheck/build when available and safe.

Task:
{task}

Architecture artifact:
{plan_summary}

Extra context:
{extra_context or "Not provided"}

{context7_note}

{_structured_instruction("implemented")}
""".strip()


def reviewer_prompt(
    task: str, plan_summary: str, implementation_summary: str, extra_context: str
) -> str:
    return f"""
You are a review participant in a Baldr-controlled durable workflow.

Hard rules:
- Do not modify files.
- Review the current Git diff against the task and architecture artifact.
- Focus on correctness, regressions, tests, security, and acceptance criteria.
- Do not delegate to Baldr or other agents.

Task:
{task}

Architecture artifact:
{plan_summary}

Implementation artifact:
{implementation_summary}

Extra context:
{extra_context or "Not provided"}

{_structured_instruction("reviewed")}
""".strip()


def fix_prompt(task: str, plan_summary: str, review_summary: str, extra_context: str) -> str:
    return f"""
You are an implementation participant in a Baldr-controlled durable fix round.

Hard rules:
- Fix only the blockers identified by review.
- Keep changes minimal.
- Do not delegate to Baldr or other agents.
- Run relevant verification when available and safe.

Task:
{task}

Architecture artifact:
{plan_summary}

Review blockers:
{review_summary}

Extra context:
{extra_context or "Not provided"}

{_structured_instruction("implemented")}
""".strip()


def _context7_note(
    workspace_root: Path,
    task: str,
    libraries: list[str] | None,
    *,
    context_config: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    settings = dict(context_config or {})
    policy = str(settings.pop("work_item_policy", "auto") or "auto").lower()
    if policy == "off":
        return "Context7 docs were disabled for this work item.", {
            "used": False,
            "enabled": False,
            "policy": "off",
        }
    if policy == "on":
        settings["enabled"] = True
        if str(settings.get("mode") or "off") == "off":
            settings["mode"] = "hybrid"
        settings["inject_docs"] = True
    bundle = prepare_context7_bundle(
        workspace_root=workspace_root,
        task_text=task,
        libraries=libraries,
        config_override=settings,
    )
    bundle["policy"] = policy
    if bundle.get("used"):
        note = (
            "Context7 documentation was prefetched and cached by Baldr. Treat it "
            "as supporting reference material; project code and tests win if they disagree.\n\n"
            + str(bundle.get("bundle") or "")
        )
    else:
        note = "Context7 docs were not injected for this step."
    return note, {key: value for key, value in bundle.items() if key != "bundle"}


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
            role.profiles = [str(item) for item in selected_profiles if str(item).strip()]
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
                # Providers consume their own field. Setting both keeps the
                # preset abstract and lets adapters ignore the irrelevant one.
                profile["reasoning_effort"] = selected_effort
                profile["effort"] = selected_effort

    wf = cfg.workflows.get(cfg.router.default_workflow, WorkflowConfig())
    rounds = max_rounds if max_rounds is not None else min(wf.max_rounds, cfg.safety.max_rounds)
    if selected_preset == "fast":
        rounds = min(int(rounds), 1)
    elif selected_preset == "deep":
        rounds = min(cfg.safety.max_rounds, max(int(rounds), int(wf.max_rounds)))
    workspace_snapshot = asdict(cfg.workspace)
    selected_workspace_mode = str(workspace_mode or "").strip().lower()
    requested_safety_mode = selected_workspace_mode or "auto"
    allow_non_git = selected_workspace_mode == "non-git"
    permission_gated_automatic = selected_workspace_mode in {
        "",
        "auto",
        "automatic",
    }
    workspace_snapshot.update(
        {
            "requested_safety_mode": requested_safety_mode,
            "allow_non_git": allow_non_git,
            "effective_require_git_repository": bool(
                cfg.workspace.require_git_repository
                and not allow_non_git
                and not permission_gated_automatic
            ),
        }
    )
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
    context7_snapshot["work_item_policy"] = str(context7_policy or "auto").strip().lower()
    if context7_snapshot["work_item_policy"] == "off":
        context7_snapshot["enabled"] = False
    elif context7_snapshot["work_item_policy"] == "on":
        context7_snapshot["enabled"] = True
        context7_snapshot["inject_docs"] = True
        if str(context7_snapshot.get("mode") or "off") == "off":
            context7_snapshot["mode"] = "hybrid"

    return {
        "engine_version": __version__,
        "execution_preset": selected_preset,
        "workflow": asdict(wf),
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
    identity = ":".join(
        [
            str(profile.get("provider") or "provider"),
            role,
            str(profile.get("model") or profile.get("agent") or "default"),
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


class DurableWorkflowEngine:
    def __init__(
        self,
        *,
        store: DurableStore | None = None,
        provider_runner: ProviderRunner = run_provider_role,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.store = store or DurableStore()
        self.provider_runner = provider_runner
        self.fault_hook = fault_hook
        self.workspace_manager = GitWorkspaceManager(self.store)

    def _fault(self, point: str, context: dict[str, Any]) -> None:
        if self.fault_hook:
            self.fault_hook(point, context)

    def recover(self) -> dict[str, Any]:
        return recover_stale_runs(self.store)

    def dry_run(
        self,
        *,
        workspace_root: Path,
        task: str,
        snapshot: dict[str, Any],
        context7_meta: dict[str, Any],
    ) -> dict[str, Any]:
        identity = workspace_identity(workspace_root)
        return {
            "ok": True,
            "dry_run": True,
            "workflow": "architect-implement-review",
            "workspace_root": str(workspace_root),
            "workspace_identity": identity,
            "role_plans": snapshot["role_plans"],
            "roles": {
                role: {
                    **plan,
                    "provider": plan["profiles"][0]["provider"],
                    "model": plan["profiles"][0].get("model"),
                    "reasoning_effort": plan["profiles"][0].get("reasoning_effort"),
                }
                for role, plan in snapshot["role_plans"].items()
            },
            "max_rounds": snapshot["max_rounds"],
            "context7": context7_meta,
            "durability": {
                "enabled": True,
                "database": str(self.store.path),
                "schema": self.store.schema_status(),
            },
            "planned_steps": [
                "architect.plan",
                "implementer.implement",
                "reviewer.review",
                "implementer.fix_blockers?",
                "reviewer.final_review?",
            ],
        }

    def request_cancel(self, run_id: str, *, reason: str = "Cancellation requested by client.") -> dict[str, Any]:
        run = self.store.request_cancellation(run_id, reason=reason)
        cleanup = terminate_processes_for_run(run_id, grace_seconds=0.75)
        if run["status"] == "cancelled":
            return self._result_from_snapshot(self.store.snapshot_run(run_id))
        owner = _owner_id()
        lease = self.store.acquire_lease(run_id, owner, max(15, self.store.config.lease_seconds))
        if lease is not None:
            try:
                self.store.finalize_cancellation(run_id, lease=lease, reason=reason)
            finally:
                self.store.release_lease(lease)
        result = self._result_from_snapshot(self.store.snapshot_run(run_id))
        result["process_cleanup"] = cleanup
        return result

    def run(
        self,
        *,
        workspace_root: Path,
        task: str,
        extra_context: str,
        config_snapshot: dict[str, Any],
        context7_libraries: list[str] | None,
        client_name: str,
        idempotency_key: str | None = None,
        resume_run_id: str | None = None,
        reconciliation_action: str | None = None,
        cancel: bool = False,
        cancel_reason: str = "Cancellation requested by client.",
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        if cancel:
            if not resume_run_id:
                return {
                    "ok": False,
                    "status": "invalid_request",
                    "error": {"code": "cancel_requires_run_id"},
                    "reason": "Cancellation requires resume_run_id.",
                }
            return self.request_cancel(resume_run_id, reason=cancel_reason)

        if config_snapshot["durability"].get("recovery_on_start", True):
            self.recover()
        if config_snapshot["durability"].get("maintenance_on_start", False):
            try:
                self.store.maintenance(full=False)
            except Exception:
                pass

        workspace_root = workspace_root.expanduser().resolve()
        owner = _owner_id()
        requested_identity = workspace_identity(workspace_root)

        try:
            if resume_run_id:
                run = self.store.get_run(resume_run_id)
                if run is None:
                    return {
                        "ok": False,
                        "status": "not_found",
                        "error": {"code": "durable_run_not_found"},
                        "reason": f"Durable run {resume_run_id!r} was not found.",
                    }
                if run["status"] in TERMINAL_RUN_STATES:
                    return self._result_from_snapshot(self.store.snapshot_run(resume_run_id))
                persisted_root = Path(str(run["workspace_root"])).expanduser().resolve()
                if workspace_root != persisted_root:
                    return {
                        "ok": False,
                        "run_id": resume_run_id,
                        "status": "resume_rejected",
                        "error": {"code": "resume_workspace_path_mismatch"},
                        "reason": "A durable run can only resume in its original workspace path.",
                        "expected_workspace_root": str(persisted_root),
                        "received_workspace_root": str(workspace_root),
                    }
                actual_identity = workspace_identity(persisted_root)
                expected_identity = run.get("repository_identity") or {}
                if expected_identity and not identities_match(expected_identity, actual_identity):
                    return {
                        "ok": False,
                        "run_id": resume_run_id,
                        "status": "resume_rejected",
                        "error": {"code": "resume_repository_identity_mismatch"},
                        "reason": (
                            "The repository at the durable workspace path is not the repository "
                            "that originally created this run."
                        ),
                        "expected_repository_fingerprint": expected_identity.get(
                            "repository_fingerprint"
                        ),
                        "actual_repository_fingerprint": actual_identity.get(
                            "repository_fingerprint"
                        ),
                    }
                requested_identity = actual_identity
                input_value = self.store.load_artifact(run.get("task_artifact_id")) or {}
                task = str(input_value.get("task") or task)
                extra_context = str(input_value.get("extra_context") or extra_context)
                context7_libraries = input_value.get("context7_libraries") or context7_libraries
                config_snapshot = run["config_snapshot"]
                run_id = str(run["id"])
            else:
                workflow_name = "architect-implement-review"
                workflow_version = int(config_snapshot["workflow"].get("version") or 1)
                fingerprint = request_fingerprint(
                    workspace=requested_identity,
                    workflow_name=workflow_name,
                    workflow_version=workflow_version,
                    task=task,
                    extra_context=extra_context,
                    context7_libraries=context7_libraries,
                    config_snapshot=config_snapshot,
                )
                candidate_id = new_run_id("workflow")
                run, created = self.store.create_run_with_input(
                    run_id=candidate_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    resume_token=f"resume-{uuid.uuid4().hex}",
                    workflow_name=workflow_name,
                    workflow_version=workflow_version,
                    workspace_root=str(workspace_root),
                    workspace_id=str(requested_identity["workspace_id"]),
                    repository_identity=requested_identity,
                    client_name=client_name,
                    input_value={
                        "task": task,
                        "extra_context": extra_context,
                        "context7_libraries": context7_libraries or [],
                    },
                    config_snapshot=config_snapshot,
                    work_item_id=work_item_id,
                )
                run_id = str(run["id"])
                if not created:
                    if run["status"] in TERMINAL_RUN_STATES:
                        return self._result_from_snapshot(self.store.snapshot_run(run_id))
                    config_snapshot = run["config_snapshot"]
                    workspace_root = Path(str(run["workspace_root"])).resolve()
                    requested_identity = run.get("repository_identity") or requested_identity
        except IdempotencyConflict as exc:
            return {
                "ok": False,
                "status": "idempotency_conflict",
                "error": {
                    "code": "idempotency_conflict",
                    "key": exc.key,
                    "expected_fingerprint": exc.expected,
                    "received_fingerprint": exc.received,
                },
                "reason": str(exc),
            }

        lease_seconds = int(config_snapshot["durability"].get("lease_seconds") or 45)
        lease = self.store.acquire_lease(run_id, owner, lease_seconds)
        if lease is None:
            return {
                "ok": False,
                "run_id": run_id,
                "status": "running",
                "reason": "Another Baldr process owns the durable workflow lease.",
            }

        started = time.time()
        release_lease = True
        try:
            run = self.store.get_run(run_id)
            assert run is not None
            if run.get("cancel_requested_at") or run["status"] == "cancelling":
                self.store.finalize_cancellation(run_id, lease=lease, reason=run.get("cancel_reason"))
                return self._result_from_snapshot(self.store.snapshot_run(run_id))

            if run["status"] == "awaiting_reconciliation":
                reconciliation = self._apply_reconciliation(
                    run=run,
                    workspace_root=workspace_root,
                    action=reconciliation_action,
                    lease=lease,
                )
                if not reconciliation.get("continue"):
                    return reconciliation["result"]
                run = self.store.get_run(run_id) or run

            if run["status"] == "pending":
                self.store.transition_run(run_id, "running", lease=lease)
            elif run["status"] in {"interrupted", "unknown"}:
                self.store.transition_run(run_id, "recovering", lease=lease)
                self.store.transition_run(run_id, "running", lease=lease)
            elif run["status"] == "recovering":
                self.store.transition_run(run_id, "running", lease=lease)

            self._fault("workflow.running", {"run_id": run_id, "lease_epoch": lease.epoch})
            context7_note, _context7_meta = _context7_note(
                workspace_root,
                task + "\n" + extra_context,
                context7_libraries,
                context_config=dict(config_snapshot.get("context7") or {}),
            )
            execution = self._restore_or_prepare_workspace(
                run_id=run_id,
                workspace_root=workspace_root,
                config_snapshot=config_snapshot,
                lease=lease,
            )
            if execution.isolated:
                violations: list[dict[str, Any]] = []
                for phase, plan in config_snapshot["role_plans"].items():
                    for profile in plan["profiles"]:
                        status = provider_isolation_status(
                            str(profile["provider"]),
                            can_write=bool(plan["can_write"]),
                            runner=str(profile.get("runner") or ""),
                            sandbox=str(plan.get("sandbox") or ""),
                        )
                        if not status["ok"]:
                            violations.append(
                                {
                                    "phase": phase,
                                    "profile": str(profile.get("name") or ""),
                                    **status,
                                }
                            )
                if violations:
                    cleanup_error: GitWorkspaceError | None = None
                    try:
                        # No provider has run yet, so this allocation contains
                        # only the verified baseline and is safe to discard.
                        self.workspace_manager.discard_workspace(execution, lease=lease)
                    except GitWorkspaceError as exc:
                        cleanup_error = exc
                    return self._finish_failed(
                        run_id,
                        started,
                        "blocked",
                        "A selected provider cannot be confined to BALDR's protected copy.",
                        {
                            "ok": False,
                            "error": {
                                "code": "provider_isolation_not_enforced",
                                "retryable": False,
                                "violations": violations,
                            },
                            "reason": (
                                "Choose a provider with an enforced workspace boundary, "
                                "or explicitly select a direct/unprotected workspace mode."
                            ),
                        },
                        lease,
                        error_code="provider_isolation_not_enforced",
                        execution=execution if cleanup_error is not None else None,
                    )
            workspace_id = str(requested_identity["workspace_id"])

            architecture = self._execute_phase(
                run_id=run_id,
                workspace_id=workspace_id,
                repository_identity=requested_identity,
                step_key="architect.plan",
                phase="architect",
                sequence_number=10,
                round_number=0,
                plan=config_snapshot["role_plans"]["architect"],
                cwd=execution.execution_root,
                prompt=architect_prompt(task, extra_context, context7_note),
                report_kind="plan",
                lease=lease,
                config_snapshot=config_snapshot,
            )
            if not architecture.get("ok"):
                return self._finish_failed(
                    run_id,
                    started,
                    "blocked",
                    "Architecture phase failed.",
                    architecture,
                    lease,
                    error_code=str(
                        architecture.get("error_code") or "workflow_phase_failed"
                    ),
                    execution=execution,
                )
            plan_summary = _extract_summary(architecture)
            write_request = _write_authorization_request(architecture)
            current_run = self.store.get_run(run_id) or {}
            authorization = str(
                (current_run.get("reconciliation") or {}).get("authorization") or ""
            ).lower()
            if (
                write_request
                and authorization != "granted"
                and _requires_write_authorization(config_snapshot)
            ):
                return self._pause_for_write_authorization(
                    run_id=run_id,
                    request=write_request,
                    execution=execution,
                    lease=lease,
                )

            implementation = self._execute_phase(
                run_id=run_id,
                workspace_id=workspace_id,
                repository_identity=requested_identity,
                step_key="implementer.implement",
                phase="implementer",
                sequence_number=20,
                round_number=0,
                plan=config_snapshot["role_plans"]["implementer"],
                cwd=execution.execution_root,
                prompt=implementer_prompt(task, plan_summary, extra_context, context7_note),
                report_kind="implementation",
                lease=lease,
                config_snapshot=config_snapshot,
                post_success=lambda step_id, reported: self.workspace_manager.checkpoint(
                    execution,
                    step_id=step_id,
                    label="implementer.implement",
                    reported_file_changes=reported,
                    lease=lease,
                ),
            )
            if not implementation.get("ok"):
                return self._finish_failed(
                    run_id,
                    started,
                    "blocked",
                    "Implementation phase failed.",
                    implementation,
                    lease,
                    error_code=str(
                        implementation.get("error_code") or "workflow_phase_failed"
                    ),
                    execution=execution,
                )
            implementation_summary = _extract_summary(implementation)

            review_result: dict[str, Any] | None = None
            rounds = int(config_snapshot.get("max_rounds") or 0)
            for round_index in range(rounds + 1):
                review_key = (
                    "reviewer.review"
                    if round_index == 0
                    else f"reviewer.review_round_{round_index}"
                )
                review_result = self._execute_phase(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    repository_identity=requested_identity,
                    step_key=review_key,
                    phase="reviewer",
                    sequence_number=30 + round_index * 20,
                    round_number=round_index,
                    plan=config_snapshot["role_plans"]["reviewer"],
                    cwd=execution.execution_root,
                    prompt=reviewer_prompt(
                        task, plan_summary, implementation_summary, extra_context
                    ),
                    report_kind="review",
                    lease=lease,
                    config_snapshot=config_snapshot,
                )
                if not review_result.get("ok"):
                    return self._finish_failed(
                        run_id,
                        started,
                        "blocked",
                        "Review phase failed.",
                        review_result,
                        lease,
                        error_code=str(
                            review_result.get("error_code")
                            or "workflow_phase_failed"
                        ),
                        execution=execution,
                    )
                if not _has_blockers(review_result):
                    break
                if round_index >= rounds:
                    break
                fix_key = f"implementer.fix_round_{round_index + 1}"
                fix_result = self._execute_phase(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    repository_identity=requested_identity,
                    step_key=fix_key,
                    phase="implementer",
                    sequence_number=40 + round_index * 20,
                    round_number=round_index + 1,
                    plan=config_snapshot["role_plans"]["implementer"],
                    cwd=execution.execution_root,
                    prompt=fix_prompt(
                        task, plan_summary, _extract_summary(review_result), extra_context
                    ),
                    report_kind="implementation",
                    lease=lease,
                    config_snapshot=config_snapshot,
                    post_success=lambda step_id, reported, key=fix_key: self.workspace_manager.checkpoint(
                        execution,
                        step_id=step_id,
                        label=key,
                        reported_file_changes=reported,
                        lease=lease,
                    ),
                )
                if not fix_result.get("ok"):
                    return self._finish_failed(
                        run_id,
                        started,
                        "blocked",
                        "Fix phase failed.",
                        fix_result,
                        lease,
                        error_code=str(
                            fix_result.get("error_code") or "workflow_phase_failed"
                        ),
                        execution=execution,
                    )
                implementation_summary = _extract_summary(fix_result)

            approved = bool(review_result and not _has_blockers(review_result))
            publication: dict[str, Any] | None = None
            if approved:
                # Publication is an externally visible, retryable finalization
                # step. Keeping it non-terminal lets recovery safely re-enter
                # an idempotent publish after a process or machine restart.
                self.store.transition_run(
                    run_id,
                    "finalizing",
                    event_type="workflow.finalization_started",
                    payload={"workspace_mode": execution.mode},
                    lease=lease,
                )
            if approved and config_snapshot["workspace"].get("publish_worktree_changes", True):
                try:
                    publication = self.workspace_manager.publish(execution, lease=lease)
                except GitWorkspaceError as exc:
                    details = self.workspace_manager.reconciliation_status(execution)
                    self.store.transition_run(
                        run_id,
                        "awaiting_reconciliation",
                        event_type="workflow.publication_requires_reconciliation",
                        payload={"reason": str(exc), "error_code": exc.code},
                        error_code=exc.code or "workspace_publication_conflict",
                        error_reason=str(exc),
                        reconciliation={
                            "reason": "workspace-publication-conflict",
                            "message": str(exc),
                            "error_code": exc.code,
                            "review_approved": True,
                            "details": exc.details,
                            **details,
                        },
                        lease=lease,
                    )
                    return self._result_from_snapshot(self.store.snapshot_run(run_id))

            final_report = {
                "status": "approved" if approved else "needs_changes",
                "summary": (
                    "Durable workflow completed with review approval."
                    if approved
                    else "Durable workflow completed but review still reports blockers."
                ),
                "architecture": plan_summary,
                "implementation": implementation_summary,
                "review": _extract_summary(review_result or {}),
                "publication": publication,
            }
            final_artifact = self.store.store_artifact(
                run_id=run_id, kind="workflow-final-report", value=final_report
            )
            target = "approved" if approved else "needs_changes"
            if not approved and execution.mode == "shadow":
                return self._retain_shadow_for_reconciliation(
                    run_id=run_id,
                    report=final_report,
                    execution=execution,
                    error_code="workflow_review_needs_changes",
                    error_reason=(
                        "Review did not approve the protected changes. Inspect, continue, "
                        "apply, or discard the durable copy."
                    ),
                    review_approved=False,
                    lease=lease,
                    final_artifact_id=final_artifact,
                )
            self.store.transition_run(
                run_id,
                target,
                final_artifact_id=final_artifact,
                payload={"duration_ms": int((time.time() - started) * 1000)},
                reconciliation={},
                lease=lease,
            )
            evidence = create_workflow_evidence(self.store, run_id)
            cleanup_requested = bool(
                approved
                and (
                    (
                        execution.mode == "worktree"
                        and config_snapshot["workspace"].get(
                            "cleanup_successful_worktrees", True
                        )
                    )
                    or (
                        execution.mode == "shadow"
                        and config_snapshot["workspace"].get(
                            "cleanup_successful_shadow_workspaces", True
                        )
                        and int(
                            config_snapshot["workspace"].get(
                                "shadow_success_retention_hours", 0
                            )
                            or 0
                        )
                        <= 0
                    )
                )
            )
            if cleanup_requested:
                try:
                    cleanup = self.workspace_manager.cleanup(execution)
                    if execution.checkpoint_id:
                        self.store.mark_checkpoint_status(
                            execution.checkpoint_id,
                            "cleaned",
                            metadata={
                                "cleaned_at": utc_now_iso(),
                                "cleanup": cleanup,
                            },
                            lease=lease,
                        )
                except GitWorkspaceError as exc:
                    # Publication is already verified. Keep durable metadata so
                    # startup maintenance can retry cleanup without changing
                    # the approved result.
                    if execution.checkpoint_id:
                        self.store.mark_checkpoint_status(
                            execution.checkpoint_id,
                            "cleanup_pending",
                            metadata={
                                "cleanup_error_code": exc.code,
                                "cleanup_error": str(exc),
                            },
                            lease=lease,
                        )
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result["evidence"] = evidence
            self._append_telemetry(result)
            return result
        except SimulatedProcessCrash:
            release_lease = False
            raise
        except WorkflowCancelled as exc:
            terminate_processes_for_run(run_id, grace_seconds=0.75)
            try:
                self.store.finalize_cancellation(run_id, lease=lease, reason=str(exc))
            except LeaseFenceError:
                pass
            return self._result_from_snapshot(self.store.snapshot_run(run_id))
        except LeaseFenceError as exc:
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result.update(
                {
                    "ok": False,
                    "stale_worker": True,
                    "reason": str(exc),
                    "error": {"code": "lease_fence_rejected"},
                }
            )
            return result
        except GitWorkspaceError as exc:
            try:
                current_run = self.store.get_run(run_id) or {}
                checkpoint = self.store.latest_checkpoint(run_id)
                execution = (
                    self.workspace_manager.from_checkpoint(checkpoint) if checkpoint else None
                )
                details = (
                    self.workspace_manager.reconciliation_status(execution)
                    if execution
                    else {"allowed_actions": ["mark_failed"]}
                )
                self.store.transition_run(
                    run_id,
                    "awaiting_reconciliation",
                    event_type="workflow.workspace_requires_reconciliation",
                    error_code=exc.code or "workspace_reconciliation_required",
                    error_reason=str(exc),
                    reconciliation={
                        "reason": "workspace-state-invalid",
                        "message": str(exc),
                        "error_code": exc.code,
                        "review_approved": bool(
                            str(current_run.get("status") or "") == "finalizing"
                            or (current_run.get("reconciliation") or {}).get(
                                "review_approved"
                            )
                        ),
                        "details": exc.details,
                        **details,
                    },
                    lease=lease,
                )
            except Exception:
                pass
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result["reason"] = str(exc)
            return result
        except Exception as exc:
            run = self.store.get_run(run_id)
            if run and run["status"] not in TERMINAL_RUN_STATES:
                try:
                    checkpoint = self.store.latest_checkpoint(run_id)
                    execution = (
                        self.workspace_manager.from_checkpoint(checkpoint)
                        if checkpoint
                        else None
                    )
                    if execution is not None and execution.mode == "shadow":
                        details = self.workspace_manager.reconciliation_status(
                            execution
                        )
                        self.store.transition_run(
                            run_id,
                            "awaiting_reconciliation",
                            event_type="workflow.shadow_retained_after_engine_failure",
                            error_code="durable_engine_failed",
                            error_reason=str(exc),
                            reconciliation={
                                "reason": "durable-engine-failure",
                                "message": str(exc),
                                "error_code": "durable_engine_failed",
                                "review_approved": bool(
                                    str(run.get("status") or "") == "finalizing"
                                    or (run.get("reconciliation") or {}).get(
                                        "review_approved"
                                    )
                                ),
                                **details,
                            },
                            lease=lease,
                        )
                    else:
                        self.store.transition_run(
                            run_id,
                            "failed",
                            error_code="durable_engine_failed",
                            error_reason=str(exc),
                            lease=lease,
                        )
                except Exception:
                    pass
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result["reason"] = str(exc)
            return result
        finally:
            if release_lease:
                self.store.release_lease(lease)

    def _apply_reconciliation(
        self,
        *,
        run: dict[str, Any],
        workspace_root: Path,
        action: str | None,
        lease: LeaseToken,
    ) -> dict[str, Any]:
        run_id = str(run["id"])
        recorded_reconciliation = run.get("reconciliation") or {}
        if recorded_reconciliation.get("reason") == "write-authorization-required":
            allowed_actions = {"authorize_changes", "decline_changes"}
            if not action:
                result = self._result_from_snapshot(self.store.snapshot_run(run_id))
                result.update(
                    {
                        "ok": False,
                        "status": "awaiting_reconciliation",
                        "reason": "Baldr necesita autorización para modificar archivos.",
                        "reconciliation": {
                            "reason": "write-authorization-required",
                            "allowed_actions": sorted(allowed_actions),
                        },
                    }
                )
                return {"continue": False, "result": result}
            action = action.strip().lower()
            if action not in allowed_actions:
                result = self._result_from_snapshot(self.store.snapshot_run(run_id))
                result.update(
                    {
                        "ok": False,
                        "status": "awaiting_reconciliation",
                        "error": {"code": "invalid_write_authorization_action"},
                        "reason": "Elegí si Baldr puede modificar archivos.",
                        "reconciliation": {
                            "reason": "write-authorization-required",
                            "allowed_actions": sorted(allowed_actions),
                        },
                    }
                )
                return {"continue": False, "result": result}
            if action == "decline_changes":
                self.store.transition_run(
                    run_id,
                    "cancelled",
                    event_type="workflow.write_authorization_declined",
                    error_code="write_authorization_declined",
                    error_reason="The person declined workspace changes.",
                    reconciliation={
                        "reason": "write-authorization-resolved",
                        "authorization": "declined",
                        "resolved_by": action,
                        "resolved_at": utc_now_iso(),
                    },
                    lease=lease,
                )
                return {
                    "continue": False,
                    "result": self._result_from_snapshot(
                        self.store.snapshot_run(run_id)
                    ),
                }
            resolved = {
                "reason": "write-authorization-resolved",
                "authorization": "granted",
                "resolved_by": action,
                "resolved_at": utc_now_iso(),
            }
            self.store.transition_run(
                run_id,
                "recovering",
                event_type="workflow.write_authorization_granted",
                reconciliation=resolved,
                lease=lease,
            )
            self.store.transition_run(
                run_id,
                "running",
                reconciliation=resolved,
                lease=lease,
            )
            return {"continue": True}

        legacy_write_authorization = self._legacy_write_policy_failure(run_id, run)
        authorization_granted = False
        if legacy_write_authorization and action in {
            "authorize_changes",
            "decline_changes",
        }:
            if action == "decline_changes":
                self.store.transition_run(
                    run_id,
                    "cancelled",
                    event_type="workflow.write_authorization_declined",
                    error_code="write_authorization_declined",
                    error_reason="The person declined workspace changes.",
                    reconciliation={
                        "reason": "write-authorization-resolved",
                        "authorization": "declined",
                        "resolved_by": action,
                        "resolved_at": utc_now_iso(),
                    },
                    lease=lease,
                )
                return {
                    "continue": False,
                    "result": self._result_from_snapshot(
                        self.store.snapshot_run(run_id)
                    ),
                }
            authorization_granted = True
            action = "continue_from_shadow"

        checkpoint = self.store.latest_checkpoint(run_id)
        execution = self.workspace_manager.from_checkpoint(checkpoint) if checkpoint else None
        workspace_config = (run.get("config_snapshot") or {}).get("workspace") or {}
        repository_identity = run.get("repository_identity") or {}
        non_git_run = bool(
            workspace_config.get("allow_non_git") is True
            or repository_identity.get("git") is False
        )
        if execution:
            details = self.workspace_manager.reconciliation_status(execution)
        elif non_git_run:
            details = {
                "allowed_actions": ["accept_existing_changes", "mark_failed"],
                "mode": "in-place",
                "original_exists": workspace_root.exists(),
                "execution_exists": workspace_root.exists(),
                "execution_is_git": False,
                "recoverable": False,
            }
        else:
            details = {"allowed_actions": ["mark_failed"]}
        if not action:
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result.update(
                {
                    "ok": False,
                    "status": "awaiting_reconciliation",
                    "reason": run.get("error_reason"),
                    "reconciliation": {**(run.get("reconciliation") or {}), **details},
                }
            )
            return {"continue": False, "result": result}
        action = action.strip().lower()
        if action not in RECONCILIATION_ACTIONS:
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result.update(
                {
                    "ok": False,
                    "status": "awaiting_reconciliation",
                    "error": {"code": "invalid_reconciliation_action"},
                    "reason": f"Unsupported reconciliation action: {action}",
                    "reconciliation": details,
                }
            )
            return {"continue": False, "result": result}
        if action not in set(details.get("allowed_actions") or []) and action != "mark_failed":
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result.update(
                {
                    "ok": False,
                    "status": "awaiting_reconciliation",
                    "error": {"code": "unsafe_reconciliation_action"},
                    "reason": f"Action {action!r} is not safe for the recorded workspace state.",
                    "reconciliation": details,
                }
            )
            return {"continue": False, "result": result}

        if action == "inspect_shadow":
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result.update(
                {
                    "ok": False,
                    "status": "awaiting_reconciliation",
                    "reason": run.get("error_reason"),
                    "reconciliation": {
                        **(run.get("reconciliation") or {}),
                        **details,
                        "inspected_at": utc_now_iso(),
                    },
                }
            )
            return {"continue": False, "result": result}

        if action == "mark_failed":
            self.store.transition_run(
                run_id,
                "failed",
                event_type="workflow.reconciliation_marked_failed",
                error_code="operator_marked_failed",
                error_reason="The operator marked an ambiguous write workflow as failed.",
                reconciliation={"resolved_by": action},
                lease=lease,
            )
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            return {"continue": False, "result": result}

        if action == "discard_shadow":
            if execution is None or execution.mode != "shadow":
                raise GitWorkspaceError(
                    "No shadow workspace exists for this reconciliation.",
                    code="shadow_workspace_missing",
                )
            self.workspace_manager.discard_workspace(execution, lease=lease)
            self.store.transition_run(
                run_id,
                "failed",
                event_type="workflow.shadow_discarded",
                error_code="operator_discarded_shadow",
                error_reason="The operator discarded the protected shadow workspace.",
                reconciliation={"resolved_by": action, "resolved_at": utc_now_iso()},
                lease=lease,
            )
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            return {"continue": False, "result": result}

        snapshot = self.store.snapshot_run(run_id, include_events=False)
        unknown_steps = [step for step in snapshot["steps"] if step["status"] == "unknown"]
        legacy_architect_step = (
            self.store.get_step(run_id, "architect.plan")
            if authorization_granted
            else None
        )
        legacy_architect_step_id = str((legacy_architect_step or {}).get("id") or "")

        if action in {
            "resume_from_checkpoint",
            "continue_from_shadow",
            "discard_worktree",
        }:
            if execution is None:
                raise GitWorkspaceError("No workspace checkpoint exists for reconciliation.")
            self.workspace_manager.restore_checkpoint(execution, lease=lease)
            for step in unknown_steps:
                self.store.reset_step_for_retry(
                    str(step["id"]),
                    reason=f"operator:{action}",
                    lease=lease,
                    retry_successful_participants=(
                        authorization_granted
                        and str(step["id"]) == legacy_architect_step_id
                    ),
                )
        elif action == "apply_shadow_changes":
            if execution is None or execution.mode != "shadow":
                raise GitWorkspaceError(
                    "No shadow checkpoint exists to apply.",
                    code="shadow_checkpoint_missing",
                )
            publication = self.workspace_manager.publish(execution, lease=lease)
            review_approved = bool((run.get("reconciliation") or {}).get("review_approved"))
            target = "approved" if review_approved else "needs_changes"
            report = {
                "status": target,
                "summary": (
                    "The operator safely applied the verified shadow checkpoint."
                ),
                "publication": publication,
                "operator_action": action,
            }
            artifact = self.store.store_artifact(
                run_id=run_id,
                kind="workflow-final-report",
                value=report,
            )
            self.store.transition_run(
                run_id,
                target,
                event_type="workflow.shadow_applied_by_operator",
                final_artifact_id=artifact,
                reconciliation={
                    "resolved_by": action,
                    "resolved_at": utc_now_iso(),
                    "publication_id": publication.get("publication_id"),
                },
                lease=lease,
            )
            cleanup_requested = bool(
                workspace_config.get("cleanup_successful_shadow_workspaces", True)
                and int(workspace_config.get("shadow_success_retention_hours", 0) or 0)
                <= 0
            )
            if cleanup_requested:
                try:
                    cleanup = self.workspace_manager.cleanup(execution)
                    if execution.checkpoint_id:
                        self.store.mark_checkpoint_status(
                            execution.checkpoint_id,
                            "cleaned",
                            metadata={"cleaned_at": utc_now_iso(), "cleanup": cleanup},
                            lease=lease,
                        )
                except GitWorkspaceError as exc:
                    if execution.checkpoint_id:
                        self.store.mark_checkpoint_status(
                            execution.checkpoint_id,
                            "cleanup_pending",
                            metadata={
                                "cleanup_error_code": exc.code,
                                "cleanup_error": str(exc),
                            },
                            lease=lease,
                        )
            result = self._result_from_snapshot(self.store.snapshot_run(run_id))
            result["evidence"] = create_workflow_evidence(self.store, run_id)
            result["publication"] = publication
            self._append_telemetry(result)
            return {"continue": False, "result": result}
        elif action == "accept_existing_changes":
            for step in unknown_steps:
                checkpoint_result = (
                    self.workspace_manager.checkpoint(
                        execution,
                        step_id=str(step["id"]),
                        label="operator-accepted-existing-changes",
                        lease=lease,
                    )
                    if execution is not None
                    else {
                        "ok": True,
                        "mode": "in-place",
                        "checkpoint_id": None,
                        "recoverable": False,
                        "observation_only": True,
                    }
                )
                report = {
                    "ok": True,
                    "status": "implemented",
                    "final_report": {
                        "status": "implemented",
                        "summary": "The operator accepted the existing workspace changes after reconciliation.",
                        "changes_added": [],
                        "changes_modified": [],
                        "changes_removed": [],
                        "files_added": [],
                        "files_modified": [],
                        "files_deleted": [],
                        "commands_run": [],
                        "tests_run": [],
                        "verification_needed": ["Review the reconciled diff before approval."],
                        "risks": [],
                        "follow_up": [],
                        "decisions": {},
                        "constraints": [],
                        "assumptions": [],
                        "alternatives_rejected": [],
                        "acceptance_criteria": [],
                        "blockers": [],
                        "review_decision": "not_applicable",
                    },
                    "checkpoint": checkpoint_result,
                    "reconciled": True,
                }
                artifact = self.store.store_artifact(
                    run_id=run_id, kind="reconciled-write-result", value=report
                )
                self.store.accept_unknown_step(
                    str(step["id"]),
                    result_artifact_id=artifact,
                    reason="operator accepted existing changes",
                    lease=lease,
                )

        if authorization_granted:
            architect_step = self.store.get_step(run_id, "architect.plan")
            if architect_step and str(architect_step.get("status") or "") in {
                "unknown",
                "interrupted",
                "failed",
            }:
                self.store.reset_step_for_retry(
                    str(architect_step["id"]),
                    reason="operator:authorize_changes:legacy-write-policy",
                    lease=lease,
                    retry_successful_participants=True,
                )

        resolved_reconciliation = {
            "resolved_by": "authorize_changes" if authorization_granted else action,
            "resolved_at": utc_now_iso(),
        }
        if authorization_granted:
            resolved_reconciliation.update(
                {
                    "reason": "write-authorization-resolved",
                    "authorization": "granted",
                }
            )
        self.store.transition_run(
            run_id,
            "recovering",
            event_type="workflow.reconciliation_resolved",
            payload={"action": action},
            reconciliation=resolved_reconciliation,
            lease=lease,
        )
        self.store.transition_run(
            run_id,
            "running",
            reconciliation=resolved_reconciliation,
            lease=lease,
        )
        return {"continue": True, "execution": execution}

    def _legacy_write_policy_failure(
        self, run_id: str, run: dict[str, Any]
    ) -> bool:
        if str(run.get("error_code") or "") not in {
            "workflow_phase_failed",
            "phase_report_blocked",
        }:
            return False
        step = self.store.get_step(run_id, "architect.plan")
        if not step:
            return False
        output = self.store.load_artifact(step.get("output_artifact_id")) or {}
        text = json.dumps(output, ensure_ascii=False).lower()
        return any(
            marker in text
            for marker in (
                "regla de no modificar archivos",
                "bloqueada por la regla de no modificar",
                "read-only planning boundary",
                "rule not to modify files",
            )
        )

    def _restore_or_prepare_workspace(
        self,
        *,
        run_id: str,
        workspace_root: Path,
        config_snapshot: dict[str, Any],
        lease: LeaseToken,
    ) -> WorkspaceExecution:
        restored = self.workspace_manager.restore_or_reconstruct(
            run_id=run_id, workspace_root=workspace_root, lease=lease
        )
        if restored is not None:
            return restored
        workspace_cfg = config_snapshot["workspace"]
        return self.workspace_manager.prepare(
            run_id=run_id,
            workspace_root=workspace_root,
            mode=str(workspace_cfg.get("write_isolation") or "auto"),
            dirty_policy=str(workspace_cfg.get("dirty_workspace_policy") or "reject"),
            workspace_config=dict(workspace_cfg),
            lease=lease,
        )

    def _execute_phase(
        self,
        *,
        run_id: str,
        workspace_id: str,
        repository_identity: dict[str, Any],
        step_key: str,
        phase: str,
        sequence_number: int,
        round_number: int,
        plan: dict[str, Any],
        cwd: Path,
        prompt: str,
        report_kind: str,
        lease: LeaseToken,
        config_snapshot: dict[str, Any],
        post_success: Callable[[str, list[dict[str, str]]], dict[str, Any]]
        | None = None,
    ) -> dict[str, Any]:
        if self.store.is_cancel_requested(run_id):
            raise WorkflowCancelled(f"Workflow {run_id} was cancelled before {step_key}.")
        existing = self.store.get_step(run_id, step_key)
        if existing and existing["status"] == "succeeded":
            return self.store.load_artifact(existing.get("output_artifact_id")) or {
                "ok": False,
                "reason": "Durable step output artifact is missing.",
            }
        if existing and existing["status"] == "unknown" and bool(existing.get("can_write")):
            return {
                "ok": False,
                "status": "unknown",
                "reason": "A previous write attempt has unknown effects and requires reconciliation.",
            }
        if existing and existing["status"] in {"interrupted", "unknown", "failed"}:
            self.store.reset_step_for_retry(
                str(existing["id"]), reason="durable resume", lease=lease
            )

        prompt_artifact = self.store.store_artifact(
            run_id=run_id,
            kind=f"{phase}-prompt-private",
            value=prompt,
            media_type="text/plain",
            redaction_level="private",
            redact=False,
        )
        step = self.store.create_step(
            run_id=run_id,
            step_key=step_key,
            phase=phase,
            sequence_number=sequence_number,
            round_number=round_number,
            strategy=str(plan["strategy"]),
            min_successes=int(plan["min_successes"]),
            can_write=bool(plan["can_write"]),
            sandbox=str(plan["sandbox"]),
            input_artifact_id=prompt_artifact,
            resolution=str(plan.get("resolution") or ""),
            min_approvals=int(plan.get("min_approvals") or 1),
            lease=lease,
        )
        step_id = str(step["id"])
        if step["status"] == "pending":
            self.store.transition_step(step_id, "dispatching", lease=lease)
            self.store.transition_step(step_id, "running", lease=lease)
        self._fault(
            f"step.{step_key}.running",
            {"run_id": run_id, "step_id": step_id, "lease_epoch": lease.epoch},
        )

        role = _role_from_plan(plan)
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        strategy = str(plan["strategy"])
        lease_seconds = int(config_snapshot["durability"].get("lease_seconds") or 45)
        heartbeat_seconds = int(config_snapshot["durability"].get("heartbeat_seconds") or 5)
        sessions_cfg = config_snapshot.get("sessions") or {}
        identity_fingerprint = str(repository_identity.get("repository_fingerprint") or "")

        for ordinal, profile in enumerate(plan["profiles"]):
            self.store.assert_lease(lease)
            if self.store.is_cancel_requested(run_id):
                raise WorkflowCancelled(f"Workflow {run_id} was cancelled during {step_key}.")
            participant = self.store.create_participant(
                step_id=step_id, ordinal=ordinal, profile=profile, lease=lease
            )
            if participant["status"] == "succeeded":
                result = self.store.load_artifact(participant.get("result_artifact_id"))
                if isinstance(result, dict):
                    successes.append(result)
                    if strategy == "first-success":
                        break
                continue

            session_key = _session_key(
                workspace_id=workspace_id,
                run_id=run_id,
                step_key=step_key,
                role=phase,
                profile=profile,
            )
            provider_identity = provider_runtime_identity(str(profile["provider"]))
            session = self.store.get_valid_session(
                session_key,
                identity_fingerprint=identity_fingerprint,
                provider_version=str(provider_identity.get("version") or ""),
                ttl_hours=int(sessions_cfg.get("ttl_hours") or 24),
                max_turns=int(sessions_cfg.get("max_turns") or 20),
                invalidate_on_identity=bool(
                    sessions_cfg.get("invalidate_on_repository_identity_change", True)
                ),
                invalidate_on_provider_version=bool(
                    sessions_cfg.get("invalidate_on_provider_version_change", True)
                ),
            )
            attempt_number = int(participant.get("attempt_count") or 0) + 1
            attempt_key = _stable_hash(
                {
                    "run": run_id,
                    "step": step_key,
                    "profile": profile["name"],
                    "attempt": attempt_number,
                }
            )
            dispatch_fp = _stable_hash(
                {
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "profile": profile,
                    "cwd": str(cwd),
                    "repository_fingerprint": identity_fingerprint,
                }
            )
            attempt, created = self.store.create_attempt(
                participant_id=str(participant["id"]),
                idempotency_key=attempt_key,
                session_key=session_key,
                owner=lease.owner,
                lease_seconds=lease_seconds,
                dispatch_fingerprint=dispatch_fp,
                lease=lease,
            )
            if not created and attempt["status"] == "succeeded":
                result = self.store.load_artifact(attempt.get("result_artifact_id"))
                if isinstance(result, dict):
                    successes.append(result)
                    if strategy == "first-success":
                        break
                continue
            attempt_id = str(attempt["id"])
            self.store.transition_attempt(attempt_id, "running", lease=lease)
            self.store.transition_participant(str(participant["id"]), "running", lease=lease)
            env = child_provider_env(
                run_id=run_id,
                workflow="architect-implement-review",
                role=phase,
                provider=str(profile["provider"]),
            )
            env.update(
                {
                    "BALDR_DURABLE_STEP_ID": step_id,
                    "BALDR_DURABLE_ATTEMPT_ID": attempt_id,
                    "BALDR_EXECUTION_PROFILE": str(profile["name"]),
                    "BALDR_LEASE_EPOCH": str(lease.epoch),
                }
            )
            heartbeat = LeaseHeartbeat(
                self.store,
                lease,
                lease_seconds,
                heartbeat_seconds,
                attempt_id=attempt_id,
            )

            def activity_sink(category: str) -> None:
                try:
                    self.store.record_phase_activity(
                        run_id=run_id,
                        step_id=step_id,
                        attempt_id=attempt_id,
                        category=category,
                        lease=lease,
                    )
                except Exception:
                    # Activity is observational and must not change provider,
                    # lease, cancellation, or recovery outcomes.
                    return

            emit_provider_activity(activity_sink, generic_activity_for_role(phase))
            with heartbeat:
                try:
                    provider_kwargs: dict[str, Any] = dict(
                        provider=str(profile["provider"]),
                        role_name=phase,
                        role=role,
                        cwd=cwd,
                        prompt=prompt,
                        workflow="architect-implement-review",
                        report_kind=report_kind,
                        extra_env=env,
                        profile_name=str(profile["name"]),
                        model=str(profile.get("model") or ""),
                        reasoning_effort=str(profile.get("reasoning_effort") or ""),
                        agent=str(profile.get("agent") or ""),
                        effort=str(profile.get("effort") or ""),
                        runner=str(profile.get("runner") or ""),
                        session_scope=str(profile.get("session_scope") or ""),
                        session_key=session_key,
                        resume_session_id=(session or {}).get("thread_id"),
                        durable_run_id=run_id,
                        durable_step_id=step_id,
                        durable_attempt_id=attempt_id,
                    )
                    if _runner_accepts_activity_sink(self.provider_runner):
                        provider_kwargs["activity_sink"] = activity_sink
                    result = self.provider_runner(**provider_kwargs)
                except (WorkflowCancelled, LeaseFenceError):
                    raise
                except Exception as exc:
                    reason = redact_text(
                        f"Provider raised {type(exc).__name__}: {exc}"
                    )
                    result = {
                        "ok": False,
                        "status": "failed",
                        "reason": reason,
                        "error": {
                            "code": "provider_unexpected_exception",
                            "message": reason,
                            "retryable": True,
                        },
                    }
            heartbeat.raise_if_unhealthy()
            self.store.assert_lease(lease)
            artifact = self.store.store_artifact(
                run_id=run_id, kind=f"{phase}-provider-result", value=result
            )
            thread_id = result.get("thread_id")
            if thread_id or profile.get("session_scope"):
                self.store.upsert_session(
                    session_key=session_key,
                    provider=str(profile["provider"]),
                    role=phase,
                    profile_name=str(profile["name"]),
                    model=str(profile.get("model") or ""),
                    runner=str(profile.get("runner") or ""),
                    thread_id=str(thread_id) if thread_id else (session or {}).get("thread_id"),
                    status="active" if result.get("ok") else "stale",
                    metadata={
                        "last_provider_run_id": result.get("run_id"),
                        "last_step_id": step_id,
                    },
                    identity_fingerprint=identity_fingerprint,
                    provider_version=str(provider_identity.get("version") or ""),
                    ttl_hours=int(sessions_cfg.get("ttl_hours") or 24),
                    increment_turn=True,
                    lease=lease,
                    run_id=run_id,
                )
            if result.get("ok"):
                self.store.transition_attempt(
                    attempt_id,
                    "succeeded",
                    provider_run_id=result.get("run_id"),
                    result_artifact_id=artifact,
                    lease=lease,
                )
                self.store.transition_participant(
                    str(participant["id"]),
                    "succeeded",
                    result_artifact_id=artifact,
                    lease=lease,
                )
                successes.append(result)
                if strategy == "first-success":
                    break
            else:
                code = (
                    (result.get("error") or {}).get("code")
                    if isinstance(result.get("error"), dict)
                    else None
                )
                reason = str(result.get("reason") or "provider execution failed")
                self.store.transition_attempt(
                    attempt_id,
                    "failed",
                    provider_run_id=result.get("run_id"),
                    result_artifact_id=artifact,
                    error_code=code,
                    error_reason=reason,
                    lease=lease,
                )
                self.store.transition_participant(
                    str(participant["id"]),
                    "failed",
                    result_artifact_id=artifact,
                    error_code=code,
                    error_reason=reason,
                    lease=lease,
                )
                failures.append(result)

        required = int(plan.get("min_successes") or 1)
        if len(successes) < required:
            output = {
                "ok": False,
                "status": "blocked",
                "error_code": "phase_min_successes_not_met",
                "reason": (
                    f"Phase {phase!r} produced {len(successes)} successful participant(s); "
                    f"{required} required."
                ),
                "participants": successes + failures,
                "resolution": {
                    "policy": plan.get("resolution"),
                    "min_successes": required,
                },
            }
            artifact = self.store.store_artifact(
                run_id=run_id, kind=f"{phase}-phase-result", value=output
            )
            self.store.transition_step(
                step_id,
                "failed",
                output_artifact_id=artifact,
                error_code="phase_min_successes_not_met",
                error_reason=output["reason"],
                lease=lease,
            )
            materialize_phase_deliverable(
                self.store,
                step_id=step_id,
                phase_output=output,
                lease=lease,
            )
            return output

        output = reduce_phase(
            phase=phase,
            participants=successes,
            policy=str(plan.get("resolution") or ""),
            min_successes=required,
            min_approvals=int(plan.get("min_approvals") or 1),
        )
        if not output.get("ok"):
            artifact = self.store.store_artifact(
                run_id=run_id, kind=f"{phase}-phase-result", value=output
            )
            reason = str(output.get("reason") or "The phase reported blockers.")
            self.store.transition_step(
                step_id,
                "failed",
                output_artifact_id=artifact,
                error_code=str(output.get("error_code") or "phase_report_blocked"),
                error_reason=reason,
                lease=lease,
            )
            materialize_phase_deliverable(
                self.store,
                step_id=step_id,
                phase_output=output,
                lease=lease,
            )
            return output
        if post_success:
            checkpoint = post_success(step_id, _reported_file_changes(output))
            output = {**output, "checkpoint": checkpoint}
        self.store.assert_lease(lease)
        artifact = self.store.store_artifact(
            run_id=run_id, kind=f"{phase}-phase-result", value=output
        )
        self.store.transition_step(
            step_id, "succeeded", output_artifact_id=artifact, lease=lease
        )
        materialize_phase_deliverable(
            self.store,
            step_id=step_id,
            phase_output=output,
            lease=lease,
        )
        self._fault(
            f"step.{step_key}.succeeded",
            {"run_id": run_id, "step_id": step_id, "lease_epoch": lease.epoch},
        )
        return output

    def _finish_failed(
        self,
        run_id: str,
        started: float,
        status: str,
        summary: str,
        detail: dict[str, Any],
        lease: LeaseToken,
        *,
        error_code: str = "workflow_phase_failed",
        execution: WorkspaceExecution | None = None,
    ) -> dict[str, Any]:
        report = {
            "status": status,
            "summary": summary,
            "detail": detail,
            "duration_ms": int((time.time() - started) * 1000),
        }
        artifact = self.store.store_artifact(
            run_id=run_id, kind="workflow-final-report", value=report
        )
        if execution is not None and execution.mode == "shadow":
            return self._retain_shadow_for_reconciliation(
                run_id=run_id,
                report=report,
                execution=execution,
                error_code=error_code,
                error_reason=summary,
                review_approved=False,
                lease=lease,
                final_artifact_id=artifact,
            )
        self.store.transition_run(
            run_id,
            status,
            final_artifact_id=artifact,
            error_code=error_code,
            error_reason=summary,
            lease=lease,
        )
        result = self._result_from_snapshot(self.store.snapshot_run(run_id))
        result["evidence"] = create_workflow_evidence(self.store, run_id)
        self._append_telemetry(result)
        return result

    def _pause_for_write_authorization(
        self,
        *,
        run_id: str,
        request: str,
        execution: WorkspaceExecution,
        lease: LeaseToken,
    ) -> dict[str, Any]:
        details = self.workspace_manager.reconciliation_status(execution)
        self.store.transition_run(
            run_id,
            "awaiting_reconciliation",
            event_type="workflow.write_authorization_requested",
            error_code="write_authorization_required",
            error_reason="Baldr necesita autorización para modificar archivos.",
            reconciliation={
                **details,
                "reason": "write-authorization-required",
                "message": "Baldr necesita autorización para modificar archivos.",
                "write_request": request,
                "allowed_actions": ["authorize_changes", "decline_changes"],
                "authorization": "pending",
            },
            lease=lease,
        )
        result = self._result_from_snapshot(self.store.snapshot_run(run_id))
        result["evidence"] = create_workflow_evidence(self.store, run_id)
        self._append_telemetry(result)
        return result

    def _retain_shadow_for_reconciliation(
        self,
        *,
        run_id: str,
        report: dict[str, Any],
        execution: WorkspaceExecution,
        error_code: str,
        error_reason: str,
        review_approved: bool,
        lease: LeaseToken,
        final_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        artifact = final_artifact_id or self.store.store_artifact(
            run_id=run_id,
            kind="workflow-final-report",
            value=report,
        )
        details = self.workspace_manager.reconciliation_status(execution)
        self.store.transition_run(
            run_id,
            "awaiting_reconciliation",
            event_type="workflow.shadow_retained_for_reconciliation",
            final_artifact_id=artifact,
            error_code=error_code,
            error_reason=error_reason,
            reconciliation={
                "reason": "shadow-work-remains",
                "message": error_reason,
                "error_code": error_code,
                "review_approved": review_approved,
                **details,
            },
            lease=lease,
        )
        result = self._result_from_snapshot(self.store.snapshot_run(run_id))
        result["evidence"] = create_workflow_evidence(self.store, run_id)
        self._append_telemetry(result)
        return result

    def _result_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        run = snapshot["run"]
        final = run.get("final") or {}
        steps = []
        for step in snapshot["steps"]:
            output = step.get("output") if isinstance(step.get("output"), dict) else {}
            profiles = [
                {
                    "name": p.get("profile_name"),
                    "provider": p.get("provider"),
                    "model": p.get("model"),
                    "reasoning_effort": p.get("reasoning_effort"),
                    "runner": p.get("runner"),
                    "status": p.get("status"),
                    "attempts": len(p.get("attempts") or []),
                }
                for p in step.get("participants", [])
            ]
            steps.append(
                {
                    "step": step.get("step_key"),
                    "phase": step.get("phase"),
                    "status": step.get("status"),
                    "strategy": step.get("strategy"),
                    "resolution": step.get("resolution"),
                    "profiles": profiles,
                    "final_report": output.get("final_report"),
                    "reason": output.get("reason"),
                }
            )
        status = str(run["status"])
        return {
            "ok": status == "approved",
            "run_id": run["id"],
            "resume_token": run.get("resume_token"),
            "workflow": run["workflow_name"],
            "workflow_version": run["workflow_version"],
            "status": status,
            "workspace_root": run["workspace_root"],
            "workspace_id": run.get("workspace_id"),
            "request_fingerprint": run.get("request_fingerprint"),
            "started_at": run["created_at"],
            "updated_at": run["updated_at"],
            "recovery_count": run["recovery_count"],
            "lease_epoch": run.get("lease_epoch"),
            "cancel_requested_at": run.get("cancel_requested_at"),
            "reconciliation": run.get("reconciliation") or {},
            "steps": steps,
            "final_report": final,
            "durable": {
                "database_path": str(self.store.path),
                "schema_version": snapshot["schema"]["schema_version"],
                "event_count": len(snapshot["events"]),
                "checkpoint_count": len(snapshot["checkpoints"]),
                "session_count": len(snapshot["sessions"]),
            },
        }

    def _append_telemetry(self, result: dict[str, Any]) -> None:
        try:
            append_run(
                {
                    "run_id": result.get("run_id"),
                    "ok": result.get("ok"),
                    "provider": "workflow",
                    "runner": result.get("workflow"),
                    "workflow": result.get("workflow"),
                    "started_at": result.get("started_at") or utc_now_iso(),
                    "status": result.get("status"),
                    "durable_schema_version": result.get("durable", {}).get("schema_version"),
                    "lease_epoch": result.get("lease_epoch"),
                    "steps": [
                        {
                            "step": step.get("step"),
                            "status": step.get("status"),
                            "profiles": step.get("profiles"),
                        }
                        for step in result.get("steps", [])
                    ],
                }
            )
        except Exception:
            pass
