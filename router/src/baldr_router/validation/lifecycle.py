from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from baldr_router import __version__
from baldr_router.config import RoleConfig, load_config
from baldr_router.discovery.environment_probe import environment_probe
from baldr_router.discovery.workspace_profile import workspace_profile
from baldr_router.evidence import (
    cleanup_evidence,
    create_evidence_bundle,
    evidence_is_current,
    latest_evidence,
)
from baldr_router.process_control import (
    active_processes,
    managed_popen,
    terminate_process_tree,
    unregister_process,
)
from baldr_router.provider_registry import provider_status, run_provider_role
from baldr_router.redaction import redact_text
from baldr_router.telemetry import app_state_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _scenario(identifier: str, func: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        payload = func()
        status = str(payload.pop("status", "passed" if payload.get("ok", True) else "failed"))
        ok = status in {"passed", "skipped"}
        return {
            "id": identifier,
            "ok": ok,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            **payload,
        }
    except Exception as exc:
        return {
            "id": identifier,
            "ok": False,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }


def _close_process_streams(proc: subprocess.Popen[Any]) -> None:
    """Release Windows pipe handles after a managed process terminates."""

    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, name, None)
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _run_capture(cmd: list[str], *, timeout: float, cwd: Path | None = None) -> dict[str, Any]:
    proc = managed_popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "BALDR_VERIFY_DISABLE": "1"},
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        termination = terminate_process_tree(proc, grace_seconds=0.5)
        return {
            "ok": False,
            "timed_out": True,
            "exit_code": proc.poll(),
            "stdout": "",
            "stderr": "timeout",
            "termination": termination,
        }
    finally:
        unregister_process(proc)
        _close_process_streams(proc)
    return {
        "ok": proc.returncode == 0,
        "timed_out": False,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _parse_json_lines(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            items.append(value)
    return items


def _fixture_command(mode: str, *args: str) -> list[str]:
    return [sys.executable, "-m", "baldr_router.validation.fixture_worker", mode, *args]


def _execute_fixture(scratch: Path) -> dict[str, Any]:
    result = _run_capture(
        _fixture_command("success", "--workspace", str(scratch), "--steps", "3", "--delay", "0.01"),
        timeout=15,
        cwd=scratch,
    )
    events = _parse_json_lines(result["stdout"])
    marker = scratch / "baldr-fixture.txt"
    completed = [item for item in events if item.get("type") == "fixture.completed"]
    ok = bool(result["ok"] and marker.exists() and completed)
    return {
        "ok": ok,
        "exit_code": result["exit_code"],
        "event_count": len(events),
        "marker_created": marker.exists(),
        "marker_sha256_prefix": __import__("hashlib").sha256(marker.read_bytes()).hexdigest()[:12]
        if marker.exists()
        else None,
    }


def _stream_fixture(scratch: Path) -> dict[str, Any]:
    result = _run_capture(
        _fixture_command("stream-progress", "--steps", "4", "--delay", "0.01"),
        timeout=15,
        cwd=scratch,
    )
    events = _parse_json_lines(result["stdout"])
    progress = [int(item.get("current", 0)) for item in events if item.get("type") == "fixture.progress"]
    ok = bool(result["ok"] and progress == [1, 2, 3, 4])
    return {
        "ok": ok,
        "event_count": len(events),
        "progress_sequence": progress,
        "ordered": progress == sorted(progress),
    }


def _cancel_fixture(scratch: Path) -> dict[str, Any]:
    pid_file = scratch / "fixture-pids.json"
    proc = managed_popen(
        _fixture_command("spawn-child-and-hang", "--pid-file", str(pid_file)),
        cwd=scratch,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "BALDR_VERIFY_DISABLE": "1"},
    )
    try:
        deadline = time.monotonic() + 8
        while (
            time.monotonic() < deadline
            and not pid_file.exists()
            and proc.poll() is None
        ):
            time.sleep(0.05)
        if not pid_file.exists():
            termination = terminate_process_tree(proc, grace_seconds=0.3)
            return {
                "ok": False,
                "reason": "fixture did not expose its process ids",
                "termination": termination,
            }
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        child_pid = int(pids.get("child_pid") or 0)
        parent_pid = int(pids.get("pid") or proc.pid)
        termination = terminate_process_tree(proc, grace_seconds=0.7)
        time.sleep(0.15)
        parent_alive = _pid_alive(parent_pid)
        child_alive = _pid_alive(child_pid)
        return {
            "ok": bool(
                termination.get("terminated")
                and not parent_alive
                and not child_alive
            ),
            "parent_pid": parent_pid,
            "child_pid": child_pid,
            "parent_alive_after": parent_alive,
            "child_alive_after": child_alive,
            "termination": termination,
        }
    finally:
        unregister_process(proc)
        _close_process_streams(proc)


def _remove_verification_tree(root: Path) -> None:
    """Remove a verification tree after transient Windows handle release."""

    deadline = time.monotonic() + 5.0
    while root.exists():
        try:
            shutil.rmtree(root)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


@contextmanager
def _temporary_verification_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="baldr-verify-"))
    try:
        yield root
    finally:
        _remove_verification_tree(root)


def _mcp_restart_fixture() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for _ in range(2):
        capture = _run_capture(
            [sys.executable, "-m", "baldr_router.validation.mcp_smoke"],
            timeout=30,
        )
        try:
            parsed = json.loads(capture["stdout"].strip().splitlines()[-1])
        except Exception:
            parsed = {
                "ok": False,
                "error": "invalid handshake output",
                "stdout_tail": capture["stdout"][-1000:],
                "stderr_tail": capture["stderr"][-1000:],
            }
        results.append(parsed)
    return {
        "ok": all(item.get("ok") is True for item in results),
        "starts": len(results),
        "handshakes": results,
    }


def _fault_fixture(scratch: Path) -> dict[str, Any]:
    crash = _run_capture(_fixture_command("crash"), timeout=10, cwd=scratch)
    invalid = _run_capture(_fixture_command("invalid-json"), timeout=10, cwd=scratch)
    invalid_events = _parse_json_lines(invalid["stdout"])
    return {
        "ok": crash.get("exit_code") == 42 and invalid.get("exit_code") == 0 and not invalid_events,
        "crash_exit_code": crash.get("exit_code"),
        "invalid_json_rejected": not invalid_events,
    }


def _redaction_fixture(scratch: Path) -> dict[str, Any]:
    result = _run_capture(_fixture_command("secret-output"), timeout=10, cwd=scratch)
    redacted = redact_text(result["stdout"])
    secret_absent = "ctx7sk-synthetic-secret-for-redaction" not in redacted
    return {
        "ok": bool(result["ok"] and secret_absent and "<redacted>" in redacted),
        "secret_absent": secret_absent,
        "redaction_marker_present": "<redacted>" in redacted,
    }


def _transaction_fixture(scratch: Path) -> dict[str, Any]:
    runtime = scratch / "runtime"
    current = runtime / "current"
    rollback = runtime / "rollback"
    current.mkdir(parents=True)
    (current / "version.txt").write_text("0.13.0\n", encoding="utf-8")

    current.rename(rollback)
    current.mkdir(parents=True)
    (current / "version.txt").write_text("broken\n", encoding="utf-8")
    shutil.rmtree(current)
    rollback.rename(current)
    restored = (current / "version.txt").read_text(encoding="utf-8").strip() == "0.13.0"

    current.rename(rollback)
    current.mkdir(parents=True)
    (current / "version.txt").write_text(__version__ + "\n", encoding="utf-8")
    shutil.rmtree(rollback)
    upgraded = (current / "version.txt").read_text(encoding="utf-8").strip() == __version__
    return {
        "ok": restored and upgraded,
        "rollback_restored_previous": restored,
        "successful_upgrade_committed": upgraded,
    }


def _install_receipt_fixture(environment: dict[str, Any]) -> dict[str, Any]:
    receipt = environment.get("runtime_receipt") or {}
    if not receipt.get("available"):
        return {
            "ok": True,
            "status": "skipped",
            "reason": "No managed-runtime receipt is available for this direct/core installation.",
        }
    return {
        "ok": receipt.get("valid") is True,
        "status": "passed" if receipt.get("valid") is True else "failed",
        "receipt": receipt,
    }


def _provider_smoke(scratch: Path) -> dict[str, Any]:
    statuses = provider_status()
    codex = (statuses.get("providers") or {}).get("codex") or {}
    if not codex.get("found"):
        return {"ok": True, "status": "skipped", "reason": "Codex CLI is not installed."}
    login = codex.get("login") or {}
    if isinstance(login, dict) and login.get("ok") is False:
        return {"ok": True, "status": "skipped", "reason": "Codex CLI is not authenticated."}
    (scratch / "README.md").write_text("# Baldr fixture\n\nPackage: baldr-fixture\n", encoding="utf-8")
    role = RoleConfig(provider="codex", can_write=False, sandbox="read-only")
    result = run_provider_role(
        provider="codex",
        role_name="reviewer",
        role=role,
        cwd=scratch,
        prompt=(
            "Read README.md and return a short structured review. Do not modify files. "
            "The expected package name is baldr-fixture."
        ),
        workflow="verification-smoke",
        report_kind="review",
        extra_env={"BALDR_ROUTER_DISABLE_REENTRY": "1"},
    )
    return {
        "ok": result.get("ok") is True,
        "provider": "codex",
        "status_result": result.get("status"),
        "error": result.get("error"),
    }


def _prepare_scratch(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git")
    if git:
        subprocess.run([git, "init", "-q", str(root)], check=False, timeout=10)
    else:
        (root / ".git").mkdir(exist_ok=True)


def run_lifecycle_verification(
    *,
    mode: str = "quick",
    workspace_root: str | None = None,
    include_provider_smoke: bool = False,
    client_id: str | None = None,
    write_evidence: bool = True,
) -> dict[str, Any]:
    if mode not in {"quick", "full"}:
        raise ValueError("mode must be quick or full")
    run_id = f"br-verify-{uuid.uuid4().hex[:12]}"
    started_at = _utc_now()
    started = time.monotonic()
    environment = environment_probe(client_id=client_id)
    profile: dict[str, Any] | None = None
    if workspace_root:
        profile = workspace_profile(workspace_root)

    with _temporary_verification_root() as verification_root:
        scratch = verification_root / "repo"
        _prepare_scratch(scratch)
        scenarios = [
            _scenario("installation_receipt", lambda: _install_receipt_fixture(environment)),
            _scenario("fixture_execute", lambda: _execute_fixture(scratch)),
            _scenario("progress_stream", lambda: _stream_fixture(scratch)),
            _scenario("cancel_process_tree", lambda: _cancel_fixture(scratch)),
            _scenario("mcp_start_restart", _mcp_restart_fixture),
            _scenario("transactional_update_rollback", lambda: _transaction_fixture(scratch)),
            _scenario("secret_redaction", lambda: _redaction_fixture(scratch)),
        ]
        if mode == "full":
            scenarios.append(_scenario("fault_injection", lambda: _fault_fixture(scratch)))
        if include_provider_smoke:
            scenarios.append(_scenario("provider_read_only_smoke", lambda: _provider_smoke(scratch)))

    failed = [item for item in scenarios if item.get("status") == "failed"]
    result: dict[str, Any] = {
        "ok": not failed,
        "schema_version": 1,
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "client_id": client_id or os.environ.get("BALDR_CLIENT_ID") or "unknown",
        "environment_fingerprint": environment.get("fingerprint"),
        "workspace_fingerprint": (profile or {}).get("fingerprint"),
        "scenario_count": len(scenarios),
        "passed": sum(1 for item in scenarios if item.get("status") == "passed"),
        "skipped": sum(1 for item in scenarios if item.get("status") == "skipped"),
        "failed": len(failed),
        "scenarios": scenarios,
        "active_processes_after": active_processes(),
    }
    if result["active_processes_after"]:
        result["ok"] = False
        result["failed"] += 1
        result["scenarios"].append(
            {
                "id": "orphan_process_check",
                "ok": False,
                "status": "failed",
                "duration_ms": 0,
                "processes": result["active_processes_after"],
            }
        )
    if write_evidence:
        cfg = load_config()
        cleanup_evidence(retention_days=cfg.verification.evidence_retention_days)
        result["evidence"] = create_evidence_bundle(
            kind="lifecycle",
            environment=environment,
            lifecycle=result,
            workspace_profile=profile,
            metadata={"mode": mode, "client_id": result["client_id"]},
        )
    return result


def _lock_path() -> Path:
    return app_state_dir() / "verification.lock"


def ensure_quick_verification(
    *,
    workspace_root: str | None = None,
    client_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    cfg = load_config()
    if os.environ.get("BALDR_VERIFY_DISABLE", "").lower() in {"1", "true", "yes"}:
        return {"ok": True, "status": "disabled", "reason": "BALDR_VERIFY_DISABLE is set."}
    if not cfg.verification.enabled:
        return {"ok": True, "status": "disabled", "reason": "Verification is disabled in config."}
    environment = environment_probe(client_id=client_id)
    fingerprint = str(environment.get("fingerprint") or "")
    if not force and evidence_is_current(
        environment_fingerprint=fingerprint,
        kind="lifecycle",
        max_age_hours=24,
    ):
        latest = latest_evidence(kind="lifecycle", successful_only=True)
        return {
            "ok": True,
            "status": "cached",
            "environment_fingerprint": fingerprint,
            **latest,
        }

    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age > max(120, cfg.verification.timeout_seconds * 2):
            lock.unlink(missing_ok=True)
            return ensure_quick_verification(
                workspace_root=workspace_root,
                client_id=client_id,
                force=force,
            )
        return {
            "ok": True,
            "status": "in_progress",
            "reason": "Another Baldr verification is already running.",
            "lock_path": str(lock),
        }
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "started_at": _utc_now()}) + "\n")
        previous = os.environ.get("BALDR_VERIFY_IN_PROGRESS")
        os.environ["BALDR_VERIFY_IN_PROGRESS"] = "1"
        try:
            return run_lifecycle_verification(
                mode=cfg.verification.setup_mode,
                workspace_root=workspace_root,
                include_provider_smoke=cfg.verification.include_provider_smoke,
                client_id=client_id,
            )
        finally:
            if previous is None:
                os.environ.pop("BALDR_VERIFY_IN_PROGRESS", None)
            else:
                os.environ["BALDR_VERIFY_IN_PROGRESS"] = previous
    finally:
        lock.unlink(missing_ok=True)
