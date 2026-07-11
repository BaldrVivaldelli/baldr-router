from __future__ import annotations

import hashlib
import json
import platform
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baldr_router import __version__
from baldr_router.discovery.environment_probe import environment_probe
from baldr_router.discovery.fingerprint import file_sha256
from baldr_router.discovery.workspace_profile import workspace_profile
from baldr_router.evidence import sanitize_evidence
from baldr_router.lab.matrix import run_lab_matrix
from baldr_router.telemetry import app_state_dir

from .definitions import canary_definition, qualification_profile, qualification_profiles
from .receipts import latest_client_receipt

QUALIFICATION_SCHEMA_VERSION = 1
ASSERTION_STATUSES = {"passed", "failed", "pending"}
CANARY_STATUSES = {"passed", "failed", "skipped", "pending"}


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
            "invalid_task_ids": [],
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
            "invalid_task_ids": ["profile-mismatch"],
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
    passed_with_evidence = [
        item
        for item in passed
        if str(item.get("run_id") or "").strip()
        and str(item.get("evidence_id") or "").strip()
        and item.get("orphan_processes") in (0, "0")
        and isinstance(item.get("tests"), list)
        and bool(item.get("tests"))
        and _task_invariants_pass(item, required_invariants)
    ]
    complete = (
        len(fingerprints) >= required_repositories
        and len(all_tasks) >= required_tasks
        and not missing_ids
        and not duplicate_ids
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
        "invalid_task_ids": invalid_ids,
        "repositories": repositories,
    }


def _provider_smoke_status(lab: dict[str, Any]) -> dict[str, Any]:
    statuses: list[str] = []
    for run in lab.get("runs") or []:
        if not isinstance(run, dict):
            continue
        for scenario in run.get("scenarios") or []:
            if isinstance(scenario, dict) and scenario.get("id") == "provider_read_only_smoke":
                statuses.append(str(scenario.get("status") or ""))
    return {
        "available": bool(statuses),
        "statuses": statuses,
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


def definitions_status() -> dict[str, Any]:
    return {
        "ok": True,
        "profiles": qualification_profiles(),
        "canaries": canary_definition(),
    }
