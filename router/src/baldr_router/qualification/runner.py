from __future__ import annotations

import hashlib
import json
import platform
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from baldr_router import __version__
from baldr_router.discovery.environment_probe import environment_probe
from baldr_router.discovery.fingerprint import file_sha256
from baldr_router.discovery.workspace_profile import workspace_profile
from baldr_router.durability.evidence import validate_workflow_evidence
from baldr_router.evidence import sanitize_evidence
from baldr_router.lab.matrix import run_lab_matrix
from baldr_router.telemetry import app_state_dir

from .definitions import canary_definition, qualification_profile, qualification_profiles
from .receipts import latest_client_receipt

QUALIFICATION_SCHEMA_VERSION = 1
ASSERTION_STATUSES = {"passed", "failed", "pending"}
CANARY_STATUSES = {"passed", "failed", "skipped", "pending"}
DEPRECATED_ASSERTIONS = {"vscode.cancel_from_ui"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def qualification_root() -> Path:
    return app_state_dir() / "qualification"


def _safe_profile(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "unknown"


def _load_json(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    source = Path(path).expanduser()
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{source} must contain a JSON object.")
    return value


def _portable(value: Any, *, workspace_root: str | None = None) -> Any:
    normalized = sanitize_evidence(value)
    workspace = str(Path(workspace_root).expanduser().resolve()) if workspace_root else ""
    if isinstance(normalized, str):
        return normalized.replace(workspace, "<workspace>") if workspace else normalized
    if isinstance(normalized, dict):
        return {
            str(key): _portable(item, workspace_root=workspace_root)
            for key, item in normalized.items()
        }
    if isinstance(normalized, list):
        return [_portable(item, workspace_root=workspace_root) for item in normalized]
    return normalized


def _canonical_bytes(value: Any, *, workspace_root: str | None = None) -> bytes:
    return json.dumps(
        _portable(value, workspace_root=workspace_root),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: Any, *, workspace_root: str | None = None) -> None:
    path.write_text(
        json.dumps(
            _portable(value, workspace_root=workspace_root),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _defined_canary_repositories() -> list[dict[str, Any]]:
    definitions = canary_definition().get("repositories") or {}
    if not isinstance(definitions, dict):
        raise ValueError("Canary definitions must contain a repositories object.")
    repositories: list[dict[str, Any]] = []
    for repository_id, raw in definitions.items():
        if not isinstance(raw, dict):
            continue
        tasks = [dict(item) for item in (raw.get("tasks") or []) if isinstance(item, dict)]
        repositories.append(
            {
                "repository_id": str(repository_id),
                "language": str(raw.get("language") or ""),
                "tasks": tasks,
            }
        )
    return repositories


def _required_canary_ids() -> set[str]:
    return {
        str(task.get("id") or "")
        for repository in _defined_canary_repositories()
        for task in repository["tasks"]
        if str(task.get("id") or "")
    }


def _canary_definitions_by_id() -> dict[str, dict[str, Any]]:
    return {
        str(task.get("id") or ""): task
        for repository in _defined_canary_repositories()
        for task in repository["tasks"]
        if str(task.get("id") or "")
    }


def _assertion_template(assertion_id: str) -> dict[str, Any]:
    return {
        "id": assertion_id,
        "status": "pending",
        "evidence": [],
        "notes": "",
    }


def qualification_template(
    profile_id: str,
    *,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    profile = qualification_profile(profile_id)
    invariants = [str(item) for item in (canary_definition().get("invariants") or [])]
    repositories: list[dict[str, Any]] = []
    for definition in _defined_canary_repositories():
        repositories.append(
            {
                "repository_id": definition["repository_id"],
                "repository_fingerprint": "",
                "display_name": definition["repository_id"],
                "language": definition["language"],
                "tasks": [
                    {
                        "id": str(item.get("id") or ""),
                        "task": str(item.get("task") or ""),
                        "accepted_run_statuses": [
                            str(status)
                            for status in (item.get("accepted_run_statuses") or [])
                        ],
                        "status": "pending",
                        "run_id": "",
                        "evidence_id": "",
                        "tests": [],
                        "orphan_processes": None,
                        "invariants": {name: None for name in invariants},
                        "notes": "",
                    }
                    for item in definition["tasks"]
                ],
            }
        )
    return {
        "ok": True,
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "profile": profile,
        "workspace_root": workspace_root or "",
        "client_assertions": {
            "schema_version": 1,
            "profile": profile_id,
            "assertions": [
                _assertion_template(item) for item in profile["all_required_assertions"]
            ],
        },
        "canary_results": {
            "schema_version": 1,
            "profile": profile_id,
            "repositories": repositories,
        },
    }


def write_qualification_template(
    profile_id: str,
    output_dir: str | Path,
    *,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    template = qualification_template(profile_id, workspace_root=workspace_root)
    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    assertions_path = target / "client-assertions.json"
    canaries_path = target / "canary-results.json"
    _write_json(assertions_path, template["client_assertions"])
    _write_json(canaries_path, template["canary_results"])
    return {
        "ok": True,
        "profile": profile_id,
        "output_dir": str(target),
        "client_assertions_path": str(assertions_path),
        "canary_results_path": str(canaries_path),
    }


def _router_is_wsl(environment: dict[str, Any]) -> bool:
    wsl = environment.get("wsl") or {}
    return bool(wsl.get("is_wsl") or wsl.get("detected"))


def _normalize_host(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "win32": "windows",
        "windows-native": "windows",
        "remote-wsl": "wsl",
        "vscode-remote-wsl": "wsl",
        "gnu/linux": "linux",
    }
    return aliases.get(normalized, normalized)


def _normalize_runtime(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "direct": "host",
        "native": "host",
        "wsl-bridge": "wsl",
        "auto-wsl": "wsl",
    }
    return aliases.get(normalized, normalized)


def _evaluate_profile_environment(
    profile: dict[str, Any],
    environment: dict[str, Any],
    receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    facts = (receipt or {}).get("facts") or {}
    if not isinstance(facts, dict):
        facts = {}
    actual_router_platform = _normalize_host(
        (environment.get("platform") or {}).get("system") or platform.system()
    )
    actual_client = str(
        (receipt or {}).get("client")
        or (environment.get("client") or {}).get("id")
        or "unknown"
    ).lower()
    actual_client_host = _normalize_host(
        facts.get("extension_host") or facts.get("host_os")
    )
    actual_router_runtime = _normalize_runtime(
        facts.get("router_runtime") or facts.get("runtime_transport")
    )
    is_wsl = _router_is_wsl(environment)
    expected_client = str(profile.get("client_id_contains") or "").lower()
    expected_host = _normalize_host(profile.get("expected_client_host"))
    expected_platform = _normalize_host(profile.get("expected_router_platform"))
    expected_runtime = _normalize_runtime(profile.get("expected_router_runtime"))
    checks = {
        "client_receipt": receipt is not None,
        "client": bool(expected_client and expected_client in actual_client),
        "client_host": actual_client_host == expected_host,
        "router_platform": actual_router_platform == expected_platform,
        "router_runtime": actual_router_runtime == expected_runtime,
        "wsl": is_wsl if profile.get("requires_wsl") else not is_wsl,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "actual": {
            "router_platform": actual_router_platform,
            "client_id": actual_client,
            "client_host": actual_client_host or None,
            "router_runtime": actual_router_runtime or None,
            "is_wsl": is_wsl,
        },
        "expected": {
            "router_platform": expected_platform,
            "client_id_contains": expected_client,
            "client_host": expected_host,
            "router_runtime": expected_runtime,
            "requires_wsl": profile.get("requires_wsl"),
        },
    }


def _evaluate_assertions(
    profile: dict[str, Any],
    value: dict[str, Any] | None,
) -> dict[str, Any]:
    required = list(profile["all_required_assertions"])
    if value is None:
        return {
            "ok": False,
            "complete": False,
            "required": required,
            "passed": [],
            "passed_with_evidence": [],
            "failed": [],
            "missing": required,
            "invalid": [],
            "items": [],
        }
    if str(value.get("profile") or "") != str(profile.get("id") or ""):
        return {
            "ok": False,
            "complete": False,
            "required": required,
            "passed": [],
            "passed_with_evidence": [],
            "failed": [],
            "missing": required,
            "invalid": ["profile-mismatch"],
            "items": [],
        }
    items = [dict(item) for item in (value.get("assertions") or []) if isinstance(item, dict)]
    by_id = {str(item.get("id") or ""): item for item in items}
    passed = [
        assertion_id
        for assertion_id in required
        if str(by_id.get(assertion_id, {}).get("status") or "").lower() == "passed"
    ]
    passed_with_evidence = [
        assertion_id
        for assertion_id in passed
        if isinstance(by_id[assertion_id].get("evidence"), list)
        and bool(by_id[assertion_id]["evidence"])
    ]
    failed = [
        assertion_id
        for assertion_id in required
        if str(by_id.get(assertion_id, {}).get("status") or "").lower() == "failed"
    ]
    missing = [
        assertion_id
        for assertion_id in required
        if assertion_id not in by_id
        or str(by_id[assertion_id].get("status") or "").lower() == "pending"
    ]
    invalid = [
        assertion_id
        for assertion_id, item in by_id.items()
        if assertion_id not in required
        or str(item.get("status") or "").lower() not in ASSERTION_STATUSES
    ]
    evidence_missing = sorted(set(passed) - set(passed_with_evidence))
    complete = not missing and not invalid and not evidence_missing
    return {
        "ok": complete and not failed and len(passed_with_evidence) == len(required),
        "complete": complete,
        "required": required,
        "passed": passed,
        "passed_with_evidence": passed_with_evidence,
        "failed": failed,
        "missing": missing,
        "evidence_missing": evidence_missing,
        "invalid": invalid,
        "items": items,
    }


def _passed_lab_scenarios(
    lab: dict[str, Any],
    scenario_id: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    runs = [item for item in (lab.get("runs") or []) if isinstance(item, dict)]
    if not runs:
        return []
    matched: list[dict[str, Any]] = []
    for run in runs:
        scenario = next(
            (
                item
                for item in (run.get("scenarios") or [])
                if isinstance(item, dict) and item.get("id") == scenario_id
            ),
            None,
        )
        if (
            scenario is None
            or scenario.get("ok") is not True
            or str(scenario.get("status") or "").lower() != "passed"
            or not predicate(scenario)
        ):
            return []
        matched.append(
            {
                "run_id": run.get("run_id"),
                "iteration": run.get("iteration"),
            }
        )
    return matched


def _automatic_assertion_evidence(
    *,
    profile: dict[str, Any],
    lab: dict[str, Any],
    receipt: dict[str, Any] | None,
    environment_check: dict[str, Any],
    workspace: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    evidence_id = str((lab.get("evidence") or {}).get("evidence_id") or "")
    automatic: dict[str, list[dict[str, Any]]] = {}

    scenario_checks: dict[
        str,
        tuple[str, Callable[[dict[str, Any]], bool]],
    ] = {
        "install.clean": (
            "installation_receipt",
            lambda item: all(
                (item.get("receipt") or {}).get(key) is True
                for key in ("valid", "executable_exists", "wheel_hash_matches")
            ),
        ),
        "mcp.handshake": (
            "mcp_start_restart",
            lambda item: int(item.get("starts") or 0) >= 2
            and bool(item.get("handshakes"))
            and all(
                isinstance(handshake, dict) and handshake.get("ok") is True
                for handshake in (item.get("handshakes") or [])
            ),
        ),
        "execution.progress_ordered": (
            "progress_stream",
            lambda item: item.get("ordered") is True,
        ),
        "cancellation.no_orphans": (
            "cancel_process_tree",
            lambda item: item.get("parent_alive_after") is False
            and item.get("child_alive_after") is False,
        ),
        "upgrade.state_preserved": (
            "transactional_update_rollback",
            lambda item: item.get("successful_upgrade_committed") is True,
        ),
        "rollback.succeeded": (
            "transactional_update_rollback",
            lambda item: item.get("rollback_restored_previous") is True,
        ),
        "secrets.clean": (
            "secret_redaction",
            lambda item: item.get("redaction_marker_present") is True
            and bool(item.get("secret_absent")),
        ),
        "restart.recovery": (
            "durable_state_contract",
            lambda item: item.get("database_reopened") is True
            and int(item.get("read_recovery_count") or 0) == 1
            and item.get("read_status") == "interrupted",
        ),
        "sqlite.local_filesystem": (
            "durable_state_contract",
            lambda item: item.get("database_is_local") is True
            and item.get("database_location") == "verification-scratch"
            and str(item.get("journal_mode") or "").lower() in {"wal", "delete"},
        ),
        "recovery.read_only": (
            "durable_state_contract",
            lambda item: item.get("read_status") == "interrupted"
            and item.get("read_step_status") == "interrupted"
            and item.get("read_attempt_status") == "interrupted",
        ),
        "recovery.write_unknown": (
            "durable_state_contract",
            lambda item: item.get("write_status") == "awaiting_reconciliation"
            and item.get("write_step_status") == "unknown"
            and item.get("write_attempt_status") == "unknown"
            and item.get("write_actions") == ["mark_failed"],
        ),
        "reconciliation.all_actions": (
            "reconciliation_actions_contract",
            lambda item: item.get("all_actions_exercised") is True
            and item.get("independent_runs") is True
            and bool(item.get("actions"))
            and all(
                isinstance(action, dict) and action.get("ok") is True
                for action in (item.get("actions") or [])
            ),
        ),
        "sessions.isolated": (
            "durable_state_contract",
            lambda item: item.get("sessions_isolated") is True,
        ),
        "lease.fencing": (
            "durable_state_contract",
            lambda item: item.get("stale_lease_rejected") is True
            and item.get("fresh_lease_accepted") is True
            and item.get("fencing_epoch_advanced") is True,
        ),
        "idempotency.conflict": (
            "durable_state_contract",
            lambda item: item.get("idempotent_replay") is True
            and item.get("idempotency_conflict_rejected") is True,
        ),
        "sqlite.maintenance": (
            "durable_state_contract",
            lambda item: item.get("maintenance_ok") is True
            and item.get("integrity_ok") is True,
        ),
        "profiles.resolved": (
            "profile_resolution_contract",
            lambda item: item.get("all_roles_resolved") is True
            and all(
                int((item.get("roles") or {}).get(role) or 0) >= 1
                for role in ("architect", "implementer", "reviewer")
            ),
        ),
    }
    for assertion_id, (scenario_id, predicate) in scenario_checks.items():
        runs = _passed_lab_scenarios(lab, scenario_id, predicate)
        if runs and evidence_id:
            automatic[assertion_id] = [
                {
                    "kind": "baldr-lab-scenario",
                    "evidence_id": evidence_id,
                    "scenario_id": scenario_id,
                    "runs": runs,
                }
            ]

    facts = (receipt or {}).get("facts") or {}
    client = str((receipt or {}).get("client") or "")
    if (
        "vscode" in client.lower()
        and facts.get("private_runtime") is True
        and "install.clean" in automatic
    ):
        automatic["vscode.extension_installed"] = [
            {
                "kind": "baldr-client-receipt",
                "client": client,
                "client_version": (receipt or {}).get("client_version"),
                "recorded_at": (receipt or {}).get("recorded_at"),
                "facts": ["private_runtime"],
            },
            *automatic["install.clean"],
        ]
    if "vscode" in client.lower() and facts.get("workspace_trusted") is True:
        automatic["vscode.workspace_trust"] = [
            {
                "kind": "baldr-client-receipt",
                "client": client,
                "recorded_at": (receipt or {}).get("recorded_at"),
                "facts": ["workspace_trusted"],
            }
        ]

    runtime_assertions = {
        "vscode-remote-wsl": "wsl.direct_runtime_selected",
        "vscode-windows-wsl": "wsl.auto_bridge_selected",
        "vscode-linux-native": "vscode.direct_runtime_selected",
        "vscode-windows-native": "vscode.direct_runtime_selected",
        "vscode-macos-native": "vscode.direct_runtime_selected",
    }
    runtime_assertion = runtime_assertions.get(str(profile.get("id") or ""))
    if runtime_assertion and environment_check.get("ok") is True:
        automatic[runtime_assertion] = [
            {
                "kind": "baldr-environment-match",
                "profile": profile.get("id"),
                "actual": environment_check.get("actual"),
                "expected": environment_check.get("expected"),
            }
        ]

    cancellation = facts.get("extension_host_cancellation") or {}
    if (
        "vscode" in client.lower()
        and isinstance(cancellation, dict)
        and cancellation.get("ok") is True
        and cancellation.get("status") == "passed"
        and cancellation.get("source") == "vscode-extension-host"
        and cancellation.get("durable_status") == "cancelled"
        and cancellation.get("worker_stopped") is True
        and type(cancellation.get("orphan_processes")) is int
        and cancellation.get("orphan_processes") == 0
        and type(cancellation.get("process_tree_observed")) is int
        and int(cancellation.get("process_tree_observed")) >= 2
        and str(cancellation.get("run_id") or "").startswith("workflow-")
        and str(cancellation.get("evidence_id") or "").startswith("br-workflow-")
        and "cancellation.no_orphans" in automatic
    ):
        automatic["vscode.cancel_from_extension_host"] = [
            {
                "kind": "baldr-vscode-extension-host-canary",
                "run_id": cancellation.get("run_id"),
                "evidence_id": cancellation.get("evidence_id"),
                "durable_status": cancellation.get("durable_status"),
                "orphan_processes": cancellation.get("orphan_processes"),
                "process_tree_observed": cancellation.get("process_tree_observed"),
            },
            *automatic["cancellation.no_orphans"],
        ]

    privacy = (workspace or {}).get("privacy") or {}
    inventory = (workspace or {}).get("inventory") or {}
    if (
        (workspace or {}).get("ok") is True
        and privacy.get("deep_source_content_read") is False
        and privacy.get("sensitive_file_patterns_excluded") is True
        and privacy.get("gitignore_respected") is True
        and privacy.get("scripts_executed") is False
        and int(inventory.get("files_considered") or 0)
        <= int(inventory.get("max_files") or 0)
    ):
        automatic["workspace.profile_bounded"] = [
            {
                "kind": "baldr-workspace-profile",
                "fingerprint": (workspace or {}).get("fingerprint"),
                "inventory_source": inventory.get("source"),
                "files_considered": inventory.get("files_considered"),
                "max_files": inventory.get("max_files"),
                "privacy": privacy,
            }
        ]
    return automatic


def _merge_automatic_assertions(
    value: dict[str, Any] | None,
    automatic: dict[str, list[dict[str, Any]]],
    *,
    profile: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    if value is None:
        return None, False
    if str(value.get("profile") or "") != str(profile.get("id") or ""):
        return value, False
    changed = False
    raw_items = value.get("assertions")
    items = raw_items if isinstance(raw_items, list) else []
    if raw_items is not items:
        value["assertions"] = items
        changed = True
    retained = [
        item
        for item in items
        if not (
            isinstance(item, dict)
            and str(item.get("id") or "") in DEPRECATED_ASSERTIONS
        )
    ]
    if len(retained) != len(items):
        items[:] = retained
        changed = True
    present = {
        str(item.get("id") or "")
        for item in items
        if isinstance(item, dict) and item.get("id")
    }
    for assertion_id in profile.get("all_required_assertions") or []:
        normalized = str(assertion_id)
        if normalized in present:
            continue
        items.append(_assertion_template(normalized))
        present.add(normalized)
        changed = True
    for item in items:
        if not isinstance(item, dict):
            continue
        assertion_id = str(item.get("id") or "")
        evidence = automatic.get(assertion_id)
        if not evidence or str(item.get("status") or "").lower() != "pending":
            continue
        item["status"] = "passed"
        item["evidence"] = evidence
        item["notes"] = "Automatically attested from this real qualification run."
        changed = True
    return value, changed


def _task_invariants_pass(task: dict[str, Any], required: list[str]) -> bool:
    values = task.get("invariants") or {}
    return isinstance(values, dict) and all(values.get(item) is True for item in required)


def _evaluate_canaries(
    profile: dict[str, Any],
    value: dict[str, Any] | None,
) -> dict[str, Any]:
    acceptance = profile.get("acceptance") or {}
    required_repositories = int(acceptance.get("required_repositories") or 2)
    required_tasks = int(acceptance.get("required_canary_tasks") or 10)
    required_ids = _required_canary_ids()
    required_invariants = [str(item) for item in (canary_definition().get("invariants") or [])]
    if value is None:
        return {
            "ok": False,
            "complete": False,
            "required_repositories": required_repositories,
            "required_tasks": required_tasks,
            "repository_count": 0,
            "task_count": 0,
            "passed_count": 0,
            "passed_with_evidence_count": 0,
            "failed_count": 0,
            "pending_count": required_tasks,
            "missing_task_ids": sorted(required_ids),
            "duplicate_task_ids": [],
            "duplicate_run_ids": [],
            "duplicate_evidence_ids": [],
            "invalid_task_ids": [],
            "invalid_evidence": [],
            "repositories": [],
        }
    if str(value.get("profile") or "") != str(profile.get("id") or ""):
        return {
            "ok": False,
            "complete": False,
            "required_repositories": required_repositories,
            "required_tasks": required_tasks,
            "repository_count": 0,
            "task_count": 0,
            "passed_count": 0,
            "passed_with_evidence_count": 0,
            "failed_count": 0,
            "pending_count": required_tasks,
            "missing_task_ids": sorted(required_ids),
            "duplicate_task_ids": [],
            "duplicate_run_ids": [],
            "duplicate_evidence_ids": [],
            "invalid_task_ids": ["profile-mismatch"],
            "invalid_evidence": [],
            "repositories": [],
        }
    repositories = [
        dict(item) for item in (value.get("repositories") or []) if isinstance(item, dict)
    ]
    all_tasks: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for repository in repositories:
        fingerprint = str(repository.get("repository_fingerprint") or "").strip()
        if fingerprint:
            fingerprints.add(fingerprint)
        for task in repository.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            normalized = dict(task)
            normalized["repository_id"] = repository.get("repository_id")
            task_id = str(normalized.get("id") or "")
            if task_id in seen_ids:
                duplicate_ids.add(task_id)
            seen_ids.add(task_id)
            all_tasks.append(normalized)
    present_ids = {str(item.get("id") or "") for item in all_tasks}
    invalid_ids = sorted(present_ids - required_ids)
    missing_ids = sorted(required_ids - present_ids)
    passed = [
        item for item in all_tasks if str(item.get("status") or "").lower() == "passed"
    ]
    failed = [
        item for item in all_tasks if str(item.get("status") or "").lower() == "failed"
    ]
    pending = [
        item
        for item in all_tasks
        if str(item.get("status") or "").lower() in {"pending", "skipped", ""}
        or str(item.get("status") or "").lower() not in CANARY_STATUSES
    ]
    definitions_by_id = _canary_definitions_by_id()
    evidence_checks: dict[str, dict[str, Any]] = {}
    for item in passed:
        task_id = str(item.get("id") or "")
        run_id = str(item.get("run_id") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not run_id or not evidence_id:
            continue
        check = validate_workflow_evidence(
            evidence_id,
            run_id=run_id,
            expected_version=__version__,
        )
        accepted_statuses = {
            str(status)
            for status in (
                definitions_by_id.get(task_id, {}).get("accepted_run_statuses") or []
            )
        }
        if (
            check.get("ok") is True
            and accepted_statuses
            and str(check.get("run_status") or "") not in accepted_statuses
        ):
            check = {
                "ok": False,
                "reason": "evidence-run-status-mismatch",
                "actual_status": check.get("run_status"),
                "accepted_statuses": sorted(accepted_statuses),
            }
        evidence_checks[task_id] = check

    run_ids = [str(item.get("run_id") or "").strip() for item in passed]
    evidence_ids = [str(item.get("evidence_id") or "").strip() for item in passed]
    duplicate_run_ids = sorted(
        {value for value in run_ids if value and run_ids.count(value) > 1}
    )
    duplicate_evidence_ids = sorted(
        {value for value in evidence_ids if value and evidence_ids.count(value) > 1}
    )
    passed_with_evidence = [
        item
        for item in passed
        if str(item.get("run_id") or "").strip()
        and str(item.get("evidence_id") or "").strip()
        and evidence_checks.get(str(item.get("id") or ""), {}).get("ok") is True
        and item.get("orphan_processes") in (0, "0")
        and isinstance(item.get("tests"), list)
        and bool(item.get("tests"))
        and _task_invariants_pass(item, required_invariants)
    ]
    invalid_evidence = [
        {
            "task_id": task_id,
            "reason": check.get("reason"),
        }
        for task_id, check in sorted(evidence_checks.items())
        if check.get("ok") is not True
    ]
    complete = (
        len(fingerprints) >= required_repositories
        and len(all_tasks) >= required_tasks
        and not missing_ids
        and not duplicate_ids
        and not duplicate_run_ids
        and not duplicate_evidence_ids
        and not invalid_ids
        and not pending
    )
    ok = complete and not failed and len(passed_with_evidence) >= required_tasks
    return {
        "ok": ok,
        "complete": complete,
        "required_repositories": required_repositories,
        "required_tasks": required_tasks,
        "repository_count": len(fingerprints),
        "task_count": len(all_tasks),
        "passed_count": len(passed),
        "passed_with_evidence_count": len(passed_with_evidence),
        "failed_count": len(failed),
        "pending_count": len(pending),
        "missing_task_ids": missing_ids,
        "duplicate_task_ids": sorted(duplicate_ids),
        "duplicate_run_ids": duplicate_run_ids,
        "duplicate_evidence_ids": duplicate_evidence_ids,
        "invalid_task_ids": invalid_ids,
        "invalid_evidence": invalid_evidence,
        "repositories": repositories,
    }


def _provider_smoke_status(lab: dict[str, Any]) -> dict[str, Any]:
    statuses: list[str] = []
    providers: list[str] = []
    for run in lab.get("runs") or []:
        if not isinstance(run, dict):
            continue
        for scenario in run.get("scenarios") or []:
            if isinstance(scenario, dict) and scenario.get("id") == "provider_read_only_smoke":
                statuses.append(str(scenario.get("status") or ""))
                provider = str(scenario.get("provider") or "").strip().lower()
                if provider:
                    providers.append(provider)
    return {
        "available": bool(statuses),
        "statuses": statuses,
        "providers": sorted(set(providers)),
        "passed": bool(statuses) and all(item == "passed" for item in statuses),
        "skipped": bool(statuses) and all(item == "skipped" for item in statuses),
    }


def _qualification_status(
    *,
    environment_ok: bool,
    lab_ok: bool,
    assertions: dict[str, Any],
    canaries: dict[str, Any],
    provider_smoke_required: bool,
    provider_smoke: dict[str, Any],
) -> str:
    if assertions.get("failed") or canaries.get("failed_count") or not lab_ok:
        return "failed"
    if provider_smoke_required and provider_smoke.get("available") and not (
        provider_smoke.get("passed") or provider_smoke.get("skipped")
    ):
        return "failed"
    provider_gate = not provider_smoke_required or bool(provider_smoke.get("passed"))
    if environment_ok and assertions.get("ok") and canaries.get("ok") and provider_gate:
        return "qualified"
    return "provisional"


def _summary_markdown(receipt: dict[str, Any]) -> str:
    checks = receipt.get("checks") or {}
    return "\n".join(
        [
            "# Baldr Router real-environment qualification",
            "",
            f"- **Qualification ID:** `{receipt.get('qualification_id')}`",
            f"- **Baldr version:** `{receipt.get('baldr_version')}`",
            f"- **Profile:** `{receipt.get('profile')}`",
            f"- **Status:** **{str(receipt.get('status')).upper()}**",
            f"- **Generated at:** `{receipt.get('generated_at')}`",
            "",
            "## Qualification gates",
            "",
            "- Environment/profile match: "
            + ("PASS" if checks.get("environment", {}).get("ok") else "PENDING/FAIL"),
            "- Three-pass lifecycle lab: "
            + ("PASS" if checks.get("lab", {}).get("ok") else "FAIL"),
            "- Real provider smoke: "
            + ("PASS" if checks.get("provider_smoke", {}).get("passed") else "PENDING/FAIL"),
            "- Client assertions: "
            f"{len(checks.get('assertions', {}).get('passed_with_evidence', []))}/"
            f"{len(checks.get('assertions', {}).get('required', []))} passed with evidence",
            "- Canary tasks: "
            f"{checks.get('canaries', {}).get('passed_with_evidence_count', 0)}/"
            f"{checks.get('canaries', {}).get('required_tasks', 0)}",
            "- Real repositories: "
            f"{checks.get('canaries', {}).get('repository_count', 0)}/"
            f"{checks.get('canaries', {}).get('required_repositories', 0)}",
            "",
            "`qualified` requires the exact real client environment, a real provider smoke, "
            "all client assertions, and ten canary tasks across two real repositories. "
            "Synthetic build validation alone remains provisional.",
            "",
            "This bundle excludes raw prompts, source code, API keys, full home paths, "
            "and raw workspace paths.",
        ]
    ) + "\n"


def _write_bundle(
    receipt: dict[str, Any],
    artifacts: dict[str, Any],
    *,
    workspace_root: str | None,
) -> dict[str, Any]:
    root = qualification_root() / str(receipt["qualification_id"])
    root.mkdir(parents=True, exist_ok=False)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    for name, value in artifacts.items():
        _write_json(root / name, value, workspace_root=workspace_root)
    _write_json(root / "receipt.json", receipt, workspace_root=workspace_root)
    (root / "summary.md").write_text(_summary_markdown(receipt), encoding="utf-8")
    hashes: dict[str, Any] = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name != "artifact-hashes.json":
            hashes[path.name] = {
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
    _write_json(root / "artifact-hashes.json", hashes)
    return {
        "ok": receipt.get("status") == "qualified",
        "qualification_id": receipt["qualification_id"],
        "status": receipt["status"],
        "path": str(root),
        "summary_path": str(root / "summary.md"),
        "receipt_sha256": receipt["receipt_sha256"],
    }


def run_qualification(
    *,
    profile_id: str,
    workspace_root: str | None = None,
    client_assertions_path: str | Path | None = None,
    canary_results_path: str | Path | None = None,
    repeat: int | None = None,
    include_provider_smoke: bool = True,
    client_id: str | None = None,
) -> dict[str, Any]:
    profile = qualification_profile(profile_id)
    latest = latest_client_receipt(family=str(profile.get("client_family") or ""))
    receipt = latest.get("receipt") if latest.get("available") else None
    environment = environment_probe(client_id=client_id or profile.get("client_id_contains"))
    environment_check = _evaluate_profile_environment(profile, environment, receipt)
    workspace = workspace_profile(workspace_root) if workspace_root else None
    required_passes = int(profile["acceptance"].get("required_consecutive_passes") or 3)
    lab = run_lab_matrix(
        repeat=repeat or required_passes,
        mode="full",
        workspace_root=workspace_root,
        include_provider_smoke=include_provider_smoke,
        profile=profile_id,
    )
    assertions_value = _load_json(client_assertions_path)
    assertions_value, assertions_changed = _merge_automatic_assertions(
        assertions_value,
        _automatic_assertion_evidence(
            profile=profile,
            lab=lab,
            receipt=receipt,
            environment_check=environment_check,
            workspace=workspace,
        ),
        profile=profile,
    )
    if assertions_changed and client_assertions_path is not None:
        _write_json(
            Path(client_assertions_path).expanduser(),
            assertions_value,
            workspace_root=workspace_root,
        )
    canaries_value = _load_json(canary_results_path)
    assertions = _evaluate_assertions(profile, assertions_value)
    canaries = _evaluate_canaries(profile, canaries_value)
    lab_ok = bool(lab.get("acceptance_met")) and int(
        lab.get("consecutive_passes") or 0
    ) >= required_passes
    provider_smoke = _provider_smoke_status(lab)
    provider_smoke_required = bool(profile["acceptance"].get("require_provider_smoke"))
    status = _qualification_status(
        environment_ok=bool(environment_check.get("ok")),
        lab_ok=lab_ok,
        assertions=assertions,
        canaries=canaries,
        provider_smoke_required=provider_smoke_required,
        provider_smoke=provider_smoke,
    )
    qualification_id = (
        f"br-qualification-{_safe_profile(profile_id)}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    core_receipt: dict[str, Any] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "qualification_id": qualification_id,
        "baldr_version": __version__,
        "profile": profile_id,
        "profile_title": profile.get("title"),
        "status": status,
        "generated_at": _utc_now(),
        "environment_fingerprint": environment.get("fingerprint"),
        "workspace_fingerprint": (workspace or {}).get("fingerprint"),
        "client_receipt_recorded_at": (receipt or {}).get("recorded_at"),
        "lab_series_id": lab.get("series_id"),
        "lab_evidence_id": (lab.get("evidence") or {}).get("evidence_id"),
        "checks": {
            "environment": environment_check,
            "lab": {
                "ok": lab_ok,
                "consecutive_passes": lab.get("consecutive_passes"),
                "required_consecutive_passes": required_passes,
            },
            "provider_smoke": {
                **provider_smoke,
                "required": provider_smoke_required,
                "requested": include_provider_smoke,
            },
            "assertions": {
                key: value for key, value in assertions.items() if key != "items"
            },
            "canaries": {
                key: value for key, value in canaries.items() if key != "repositories"
            },
        },
        "privacy": {
            "raw_prompts_included": False,
            "source_code_included": False,
            "secret_values_included": False,
            "raw_workspace_path_included": False,
        },
    }
    core_receipt["receipt_sha256"] = hashlib.sha256(
        _canonical_bytes(core_receipt, workspace_root=workspace_root)
    ).hexdigest()
    bundle = _write_bundle(
        core_receipt,
        {
            "environment.json": environment,
            "client-receipt.json": receipt or {"available": False},
            "workspace-profile.json": workspace or {"available": False},
            "lab-result.json": lab,
            "client-assertions.json": assertions_value or {"available": False},
            "canary-results.json": canaries_value or {"available": False},
            "requirements.json": {
                "profile": profile,
                "canary_definition": canary_definition(),
            },
        },
        workspace_root=workspace_root,
    )
    next_steps: list[str] = []
    if not environment_check.get("ok"):
        next_steps.append("Run qualification from the exact target client/runtime profile.")
    if provider_smoke_required and not provider_smoke.get("passed"):
        next_steps.append("Authenticate a real provider and rerun with provider smoke enabled.")
    if not assertions.get("ok"):
        next_steps.append("Complete every client assertion with at least one evidence reference.")
    if not canaries.get("ok"):
        next_steps.append(
            "Record ten passed canary tasks with evidence across two distinct real repositories."
        )
    return {
        "ok": status == "qualified",
        "status": status,
        "qualification_id": qualification_id,
        "profile": profile_id,
        "checks": core_receipt["checks"],
        "receipt_sha256": core_receipt["receipt_sha256"],
        "bundle": bundle,
        "next_steps": next_steps,
    }


def _load_receipt(directory: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return {**value, "path": str(directory)} if isinstance(value, dict) else None


def list_qualifications(*, limit: int = 20) -> dict[str, Any]:
    root = qualification_root()
    if not root.exists():
        return {"ok": True, "path": str(root), "count": 0, "items": []}
    items = [
        item
        for item in (
            _load_receipt(path) for path in root.iterdir() if path.is_dir()
        )
        if item
    ]
    items.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    return {
        "ok": True,
        "path": str(root),
        "count": len(items),
        "items": items[: max(1, min(limit, 100))],
    }


def latest_qualification(
    *,
    profile_id: str | None = None,
    qualified_only: bool = False,
) -> dict[str, Any]:
    for item in list_qualifications(limit=100)["items"]:
        if profile_id and item.get("profile") != profile_id:
            continue
        if qualified_only and item.get("status") != "qualified":
            continue
        return {"ok": True, "available": True, "qualification": item}
    return {
        "ok": True,
        "available": False,
        "qualification": None,
        "path": str(qualification_root()),
    }


def qualification_receipt_sha256(receipt: dict[str, Any]) -> str:
    """Return the canonical digest used by a persisted qualification receipt."""
    core = {
        key: value
        for key, value in receipt.items()
        if key not in {"path", "receipt_sha256"}
    }
    return hashlib.sha256(_canonical_bytes(core)).hexdigest()


def _promotion_receipt_files(receipt_paths: list[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in receipt_paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file():
            files.append(path)
            continue
        if path.is_dir():
            direct = path / "receipt.json"
            if direct.is_file():
                files.append(direct)
            else:
                files.extend(sorted(path.rglob("receipt.json")))
            continue
        raise FileNotFoundError(f"Qualification receipt path does not exist: {path}")
    return list(dict.fromkeys(files))


def promotion_status(
    *,
    receipt_paths: list[str | Path] | None = None,
    release_version: str | None = None,
) -> dict[str, Any]:
    """Verify the exact real-environment receipts required for promotion."""
    definitions = qualification_profiles()
    policy = dict(definitions.get("promotion") or {})
    required_profiles = [str(item) for item in (policy.get("required_profiles") or [])]
    provider = str(policy.get("provider") or "").strip().lower()
    expected_version = str(release_version or __version__)

    if receipt_paths:
        receipts: list[dict[str, Any]] = []
        for path in _promotion_receipt_files(receipt_paths):
            value = _load_json(path)
            if value is not None:
                receipts.append({**value, "path": str(path)})
    else:
        receipts = list(list_qualifications(limit=100)["items"])

    evaluations: list[dict[str, Any]] = []
    accepted: dict[str, dict[str, Any]] = {}
    known_profiles = set((definitions.get("profiles") or {}).keys())
    for receipt in receipts:
        profile = str(receipt.get("profile") or "")
        errors: list[str] = []
        if int(receipt.get("schema_version") or 0) != QUALIFICATION_SCHEMA_VERSION:
            errors.append("schema-version-mismatch")
        if profile not in known_profiles:
            errors.append("unknown-profile")
        if str(receipt.get("status") or "") != "qualified":
            errors.append("receipt-not-qualified")
        if str(receipt.get("baldr_version") or "") != expected_version:
            errors.append("release-version-mismatch")
        supplied_digest = str(receipt.get("receipt_sha256") or "")
        if not supplied_digest or supplied_digest != qualification_receipt_sha256(receipt):
            errors.append("receipt-digest-mismatch")
        provider_check = ((receipt.get("checks") or {}).get("provider_smoke") or {})
        providers = {
            str(item).strip().lower()
            for item in (provider_check.get("providers") or [])
            if str(item).strip()
        }
        if profile in required_profiles:
            if provider_check.get("passed") is not True:
                errors.append("provider-smoke-not-passed")
            if provider and provider not in providers:
                errors.append("promotion-provider-mismatch")

        required = profile in required_profiles
        eligible = required and not errors
        evaluation = {
            "profile": profile or None,
            "qualification_id": receipt.get("qualification_id"),
            "status": receipt.get("status"),
            "receipt_sha256": supplied_digest or None,
            "path": receipt.get("path"),
            "required": required,
            "eligible": eligible,
            "errors": errors,
        }
        evaluations.append(evaluation)
        if eligible and profile not in accepted:
            accepted[profile] = evaluation

    missing = [profile for profile in required_profiles if profile not in accepted]
    return {
        "ok": bool(required_profiles) and not missing,
        "release_version": expected_version,
        "policy": {
            "provider": provider,
            "required_profiles": required_profiles,
            "deferred_profiles": [
                str(item) for item in (policy.get("deferred_profiles") or [])
            ],
            "note": policy.get("note"),
        },
        "accepted_profiles": sorted(accepted),
        "missing_profiles": missing,
        "receipts": evaluations,
    }


def definitions_status() -> dict[str, Any]:
    return {
        "ok": True,
        "profiles": qualification_profiles(),
        "canaries": canary_definition(),
    }
