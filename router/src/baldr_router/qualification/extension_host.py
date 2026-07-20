from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from baldr_router.config import AppConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.store import DurableStore
from baldr_router.process_control import active_processes, managed_popen, unregister_process


def _report(role: str) -> dict[str, Any]:
    status = {
        "architect": "planned",
        "implementer": "implemented",
        "reviewer": "approved",
    }[role]
    return {
        "status": status,
        "summary": f"Deterministic {role} qualification fixture.",
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"write_authorization": "not_required"},
        "review_decision": "approved" if role == "reviewer" else "not_applicable",
    }


def _git_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("extension-host cancellation fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Baldr Qualification",
            "-c",
            "user.email=qualification@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "qualification fixture",
        ],
        check=True,
    )
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.name != "nt":
        stat = Path(f"/proc/{pid}/stat")
        try:
            if stat.read_text(encoding="utf-8").split()[2] == "Z":
                return False
        except (FileNotFoundError, IndexError, OSError):
            pass
    return True


def _snapshot(workspace: Path) -> dict[str, Any]:
    config = AppConfig.defaults()
    config.context7.enabled = False
    config.context7.mode = "off"
    workflow = config.workflows["architect-implement-review"]
    workflow.max_rounds = 0
    workflow.max_parallel_participants = 1
    return _resolved_snapshot(
        config,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="current",
        context7_policy="off",
        execution_preset="fast",
        team_mode="configured",
        workspace_root=workspace,
    )


def run_extension_host_cancellation_canary(
    *,
    client: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Exercise durable cancellation for an invocation owned by VS Code.

    The provider is deterministic and local. It creates a real child process
    tree registered against the durable run, blocks in the implementation
    phase, and is cancelled through the same engine entrypoint used by the
    VS Code runtime. The returned evidence contains no workspace or source
    paths and can be embedded safely in a client receipt.
    """

    normalized_client = str(client or "").strip().lower()
    if "vscode" not in normalized_client:
        return {
            "ok": False,
            "status": "invalid_client",
            "reason": "The extension-host cancellation canary requires a VS Code client.",
        }

    with tempfile.TemporaryDirectory(prefix="baldr-vscode-cancel-") as temporary:
        root = Path(temporary)
        workspace = _git_repository(root / "workspace")
        store = DurableStore(path=root / "cancellation.sqlite3")
        implementation_started = threading.Event()
        pid_file = root / "fixture-pids.json"
        provider_error: list[str] = []

        def provider(**kwargs: Any) -> dict[str, Any]:
            role = str(kwargs["role_name"])
            if role != "implementer":
                return {
                    "ok": True,
                    "run_id": f"qualification-{role}",
                    "final_report": _report(role),
                }

            command = [
                sys.executable,
                "-m",
                "baldr_router.validation.fixture_worker",
                "spawn-child-and-hang",
                "--pid-file",
                str(pid_file),
            ]
            environment = {
                **os.environ,
                **(kwargs.get("extra_env") or {}),
                "BALDR_VERIFY_DISABLE": "1",
            }
            process = managed_popen(
                command,
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and process.poll() is None:
                    if pid_file.exists():
                        implementation_started.set()
                        break
                    time.sleep(0.025)
                if not implementation_started.is_set():
                    provider_error.append("fixture_process_tree_not_ready")
                process.wait(timeout=max(1.0, timeout_seconds))
            except subprocess.TimeoutExpired:
                provider_error.append("fixture_process_tree_not_cancelled")
            finally:
                unregister_process(process)
            return {
                "ok": True,
                "run_id": "qualification-implementer",
                "final_report": _report(role),
            }

        engine = DurableWorkflowEngine(store=store, provider_runner=provider)
        completed: dict[str, Any] = {}

        def execute() -> None:
            completed["result"] = engine.run(
                workspace_root=workspace,
                task="Exercise automated cancellation from the VS Code Extension Host.",
                extra_context="",
                config_snapshot=_snapshot(workspace),
                context7_libraries=None,
                client_name=normalized_client,
            )

        worker = threading.Thread(
            target=execute,
            name="vscode-extension-host-cancellation-canary",
            daemon=True,
        )
        worker.start()
        if not implementation_started.wait(timeout=min(10.0, timeout_seconds)):
            worker.join(timeout=1.0)
            return {
                "ok": False,
                "status": "fixture_not_ready",
                "reason": provider_error[-1] if provider_error else "implementation did not start",
            }

        row = store.connect().execute(
            "SELECT id FROM workflow_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"ok": False, "status": "run_not_persisted"}
        run_id = str(row["id"])
        cancel_result = engine.request_cancel(
            run_id,
            reason="Automated cancellation requested by the VS Code Extension Host.",
        )
        worker.join(timeout=max(2.0, timeout_seconds))

        final_result = completed.get("result") or cancel_result
        manifest = ((final_result.get("evidence") or {}).get("manifest") or {})
        pids: dict[str, Any] = {}
        try:
            pids = json.loads(pid_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            provider_error.append("fixture_pid_evidence_missing")
        observed_pids = [int(pids.get(key) or 0) for key in ("pid", "child_pid")]
        orphan_pids = [pid for pid in observed_pids if _pid_alive(pid)]
        registered = active_processes(run_id=run_id)
        durable_status = str(final_result.get("status") or "")
        evidence_id = str(manifest.get("evidence_id") or "")
        ok = bool(
            not worker.is_alive()
            and durable_status == "cancelled"
            and evidence_id
            and not orphan_pids
            and not registered
            and not provider_error
        )
        return {
            "ok": ok,
            "status": "passed" if ok else "failed",
            "source": "vscode-extension-host",
            "client": normalized_client,
            "run_id": run_id,
            "evidence_id": evidence_id,
            "durable_status": durable_status,
            "cancel_requested": bool(manifest.get("cancel_requested_at")),
            "orphan_processes": len(orphan_pids) + len(registered),
            "process_tree_observed": len([pid for pid in observed_pids if pid > 0]),
            "worker_stopped": not worker.is_alive(),
            "errors": provider_error,
        }
