from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from baldr_agent_sdk.contract import (
    CONTRACT,
    VERSION,
    ContractError,
    canonical_digest,
    canonical_json,
    parse_message,
    validate_digest,
)

from .store import RunnerStore

_ALLOWED_TARGET_KEYS = {
    "command",
    "arguments_json",
    "artifact_path",
    "artifact_digest",
    "protocol",
    "timeout_seconds",
}
_TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
_SAFE_ENV = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TMP",
    "TEMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
}
_SNAPSHOT_EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".gradle",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "venv",
}


class RunnerError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _execution_message(kind: str, request_id: str, **values: Any) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "kind": kind,
        "request_id": request_id,
        **values,
    }


def _emit(output: TextIO, payload: Mapping[str, Any]) -> None:
    output.write(canonical_json(dict(payload)) + "\n")
    output.flush()


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _terminate(proc: subprocess.Popen[str], grace: float = 1.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _safe_target(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerError("Runner target must be an object.", code="runner_target_invalid")
    unexpected = sorted(set(value) - _ALLOWED_TARGET_KEYS)
    if unexpected:
        raise RunnerError(
            "Runner target contains unsupported fields: " + ", ".join(unexpected),
            code="runner_target_invalid",
        )
    required = _ALLOWED_TARGET_KEYS
    missing = sorted(key for key in required if not str(value.get(key) or "").strip())
    if missing:
        raise RunnerError(
            "Runner target is missing: " + ", ".join(missing),
            code="runner_target_invalid",
        )
    if value.get("protocol") != "stdio-jsonl-v1":
        raise RunnerError("Unsupported local agent protocol.", code="runner_protocol_invalid")
    try:
        arguments = json.loads(str(value["arguments_json"]))
    except json.JSONDecodeError as exc:
        raise RunnerError("arguments_json is invalid.", code="runner_target_invalid") from exc
    if (
        not isinstance(arguments, list)
        or len(arguments) > 64
        or any(not isinstance(item, str) or len(item) > 4096 for item in arguments)
    ):
        raise RunnerError("Agent arguments are invalid.", code="runner_target_invalid")
    try:
        timeout = int(str(value["timeout_seconds"]))
    except ValueError as exc:
        raise RunnerError("Agent timeout is invalid.", code="runner_target_invalid") from exc
    if not 1 <= timeout <= 86400:
        raise RunnerError("Agent timeout is invalid.", code="runner_target_invalid")
    artifact = Path(str(value["artifact_path"])).expanduser().resolve()
    if not artifact.is_file() or artifact.is_symlink():
        raise RunnerError(
            "Pinned agent artifact is unavailable.", code="runner_artifact_unavailable"
        )
    expected = validate_digest(value["artifact_digest"], "artifact_digest")
    actual = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual != expected:
        raise RunnerError(
            "Pinned agent artifact digest changed.", code="runner_artifact_digest_mismatch"
        )
    command_value = str(value["command"]).strip()
    resolved_command = shutil.which(command_value)
    if not resolved_command:
        candidate = Path(command_value).expanduser()
        if candidate.is_file():
            resolved_command = str(candidate.resolve())
    if not resolved_command:
        raise RunnerError("Agent command was not found.", code="runner_command_not_found")
    if str(artifact) != str(Path(resolved_command).resolve()) and str(artifact) not in arguments:
        raise RunnerError(
            "The pinned artifact must be the executable or an exact command argument.",
            code="runner_artifact_not_invoked",
        )
    return {
        "command": resolved_command,
        "arguments": arguments,
        "artifact": artifact,
        "artifact_digest": actual,
        "timeout_seconds": timeout,
    }


def _copy_read_only_workspace(source: Path, destination: Path) -> str:
    if not source.is_dir() or source.is_symlink():
        raise RunnerError("Workspace must be a real directory.", code="runner_workspace_invalid")
    file_count = 0
    total_bytes = 0
    digest = hashlib.sha256()
    destination.mkdir(mode=0o700)
    for root, directories, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        # A snapshot is a deliberately reduced view, not a validator for the
        # original tree. Common dependency/build directories and links are
        # omitted so ordinary Python, Node and Rust workspaces remain usable
        # without ever copying or following an alternate filesystem path.
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SNAPSHOT_EXCLUDED_DIRECTORIES
            and not (root_path / name).is_symlink()
        )
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            child = root_path / name
            if child.is_symlink() or not child.is_file():
                continue
            size = child.stat().st_size
            file_count += 1
            total_bytes += size
            if file_count > 100_000 or size > 512 * 1024 * 1024 or total_bytes > 5 * 1024**3:
                raise RunnerError("Workspace exceeds runner snapshot limits.", code="runner_workspace_too_large")
            relative = child.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target, follow_symlinks=False)
            mode = stat.S_IMODE(target.stat().st_mode) & ~0o222
            target.chmod(mode)
            digest.update(relative.as_posix().encode("utf-8") + b"\0")
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    for root, directories, _files in os.walk(destination, topdown=False):
        for name in directories:
            path = Path(root) / name
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    destination.chmod(0o500)
    return "sha256:" + digest.hexdigest()


class LocalAgentRunner:
    def __init__(self, *, store: RunnerStore | None = None) -> None:
        self.store = store or RunnerStore()
        self._active_lock = threading.Lock()
        self._active: subprocess.Popen[str] | None = None
        self._cancel_requested = threading.Event()

    def handle(
        self,
        raw_message: Mapping[str, Any],
        *,
        output: TextIO,
        target: Mapping[str, Any] | None = None,
    ) -> int:
        message = parse_message(raw_message)
        kind = message["kind"]
        if kind == "health-request":
            _emit(
                output,
                _execution_message(
                    "health-response",
                    message["request_id"],
                    status="ok",
                    runner_version="0.20.0",
                    protocols=[1],
                ),
            )
            return 0
        if kind == "status-request":
            return self._status(message, output)
        if kind == "events-request":
            return self._events(message, output)
        if kind == "cancel":
            return self._cancel(message, output)
        if kind != "invoke":
            raise RunnerError("Runner accepts request messages only.", code="runner_request_invalid")
        return self._invoke(message, output=output, target=_safe_target(target))

    def _status(self, message: Mapping[str, Any], output: TextIO) -> int:
        job = self.store.get(str(message["job_id"]))
        if job is None:
            raise RunnerError("Runner job was not found.", code="runner_job_not_found")
        events = self.store.events(str(message["job_id"]), after=0, limit=1000)
        _emit(
            output,
            _execution_message(
                "status",
                str(message["request_id"]),
                job_id=str(message["job_id"]),
                state=job["state"],
                event_cursor=events[-1]["sequence"] if events else 0,
                error_code=job.get("error_code"),
            ),
        )
        return 0

    def _events(self, message: Mapping[str, Any], output: TextIO) -> int:
        if self.store.get(str(message["job_id"])) is None:
            raise RunnerError("Runner job was not found.", code="runner_job_not_found")
        for event in self.store.events(
            str(message["job_id"]),
            after=int(message.get("after") or 0),
            limit=int(message.get("limit") or 100),
        ):
            _emit(
                output,
                _execution_message(
                    "event",
                    str(message["request_id"]),
                    job_id=str(message["job_id"]),
                    sequence=event["sequence"],
                    category=event["category"],
                    message=event["message"],
                ),
            )
        return 0

    def _cancel(self, message: Mapping[str, Any], output: TextIO) -> int:
        job_id = str(message["job_id"])
        job = self.store.get(job_id)
        if job is None:
            raise RunnerError("Runner job was not found.", code="runner_job_not_found")
        pid = int(job.get("child_pid") or 0)
        state = "unknown" if job["effect_mode"] == "workspace-write" else "cancelled"
        error = {
            "code": "agent_write_effect_unknown" if state == "unknown" else "agent_cancelled",
            "message": "Agent cancellation was requested.",
            "retryable": state == "cancelled",
        }
        result = self._result_message(job, state=state, body={"ok": False}, error=error)
        # Publish the terminal decision before signalling the child. The
        # invocation thread can wake up immediately after SIGTERM; persisting
        # first prevents it from racing cancellation with process_failed.
        self.store.finish(job_id, state=state, result=result, error_code=error["code"])
        if job["state"] not in _TERMINAL and _pid_alive(pid):
            try:
                if os.name == "nt":
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        self.store.add_event(job_id, "cancelled", str(message.get("reason") or ""))
        _emit(output, result)
        return 0

    def _result_message(
        self,
        job: Mapping[str, Any],
        *,
        state: str,
        body: Mapping[str, Any],
        error: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return _execution_message(
            "result",
            str(job["request_id"]),
            job_id=str(job["job_id"]),
            state=state,
            agent={"ref": job["agent_ref"], "digest": job["agent_digest"]},
            result=dict(body),
            error=dict(error) if error is not None else None,
        )

    def _wait_for_existing(self, job: Mapping[str, Any], timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        current = dict(job)
        while current["state"] not in _TERMINAL and time.monotonic() < deadline:
            pid = int(current.get("child_pid") or 0)
            if current["state"] == "running" and pid and not _pid_alive(pid):
                state = "unknown" if current["effect_mode"] == "workspace-write" else "failed"
                error = {
                    "code": "agent_write_effect_unknown" if state == "unknown" else "agent_process_lost",
                    "message": "The previous runner process ended without a durable result.",
                    "retryable": state == "failed",
                }
                result = self._result_message(current, state=state, body={"ok": False}, error=error)
                self.store.finish(current["job_id"], state=state, result=result, error_code=error["code"])
                return result
            time.sleep(0.05)
            current = self.store.get(current["job_id"]) or current
        if current.get("result_json"):
            return json.loads(current["result_json"])
        raise RunnerError("Idempotent job is still running.", code="runner_job_running", retryable=True)

    def _invoke(
        self,
        message: Mapping[str, Any],
        *,
        output: TextIO,
        target: Mapping[str, Any],
    ) -> int:
        invocation = dict(message["invocation"])
        workspace = dict(invocation["workspace"])
        effect_mode = str(workspace["effect_mode"])
        if effect_mode == "workspace-write" and "workspace.write" not in invocation["requested_capabilities"]:
            raise RunnerError("Write mode requires workspace.write.", code="runner_policy_denied")
        request_digest = canonical_digest(message)
        identity = message["agent"]
        try:
            job, reused = self.store.begin(
                job_id=str(message["job_id"]),
                idempotency_key=str(message["idempotency_key"]),
                request_digest=request_digest,
                request_id=str(message["request_id"]),
                agent_ref=str(identity["ref"]),
                agent_digest=str(identity["digest"]),
                effect_mode=effect_mode,
            )
        except ValueError as exc:
            raise RunnerError(str(exc), code="runner_idempotency_conflict") from exc
        _emit(
            output,
            _execution_message(
                "accepted",
                str(message["request_id"]),
                job_id=str(message["job_id"]),
                state="running" if reused and job["state"] == "running" else "accepted",
                reused=reused,
            ),
        )
        if reused:
            result = self._wait_for_existing(job, int(target["timeout_seconds"]))
            _emit(output, result)
            return 0 if result["state"] == "succeeded" else 1

        if not isinstance(workspace.get("root"), str) or not workspace["root"].strip():
            raise RunnerError(
                "Local execution requires an explicit workspace root.",
                code="runner_workspace_invalid",
            )
        original_root = Path(workspace["root"]).expanduser().resolve()
        if not original_root.is_dir() or original_root.is_symlink():
            raise RunnerError("Workspace is unavailable.", code="runner_workspace_invalid")
        temp: tempfile.TemporaryDirectory[str] | None = None
        execution_root = original_root
        try:
            if effect_mode == "read-only":
                temp = tempfile.TemporaryDirectory(prefix="baldr-agent-read-")
                execution_root = Path(temp.name) / "workspace"
                workspace["scope_digest"] = _copy_read_only_workspace(original_root, execution_root)
            invocation["workspace"] = {**workspace, "root": str(execution_root)}
            child_message = {**dict(message), "invocation": invocation}
            env = {key: value for key, value in os.environ.items() if key.upper() in _SAFE_ENV}
            env.update(
                {
                    "BALDR_AGENT_JOB_ID": str(message["job_id"]),
                    "BALDR_AGENT_REF": str(identity["ref"]),
                    "BALDR_AGENT_WORKSPACE": str(execution_root),
                    "BALDR_AGENT_EFFECT_MODE": effect_mode,
                }
            )
            if temp is not None:
                agent_home = Path(temp.name) / "home"
                agent_home.mkdir(mode=0o700)
                env["HOME"] = str(agent_home)
            proc = subprocess.Popen(
                [str(target["command"]), *list(target["arguments"])],
                cwd=execution_root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            with self._active_lock:
                self._active = proc
            self.store.set_running(str(message["job_id"]), proc.pid)
            self.store.add_event(str(message["job_id"]), "runner.started", "")
            try:
                stdout, stderr = proc.communicate(
                    canonical_json(child_message) + "\n",
                    timeout=min(
                        int(invocation["timeout_seconds"]),
                        int(target["timeout_seconds"]),
                    ),
                )
            except subprocess.TimeoutExpired:
                _terminate(proc)
                state = "unknown" if effect_mode == "workspace-write" else "failed"
                error = {
                    "code": "agent_write_effect_unknown" if state == "unknown" else "agent_timeout",
                    "message": "External agent exceeded its timeout.",
                    "retryable": state == "failed",
                }
                result = self._result_message(job, state=state, body={"ok": False}, error=error)
                self.store.finish(job["job_id"], state=state, result=result, error_code=error["code"])
                _emit(output, result)
                return 1
            finally:
                with self._active_lock:
                    self._active = None

            # Cancellation is handled through a second protocol request and
            # durably wins the race with the original invocation. The child
            # may exit without a result after SIGTERM; never overwrite the
            # already-recorded cancelled/unknown state with process_failed.
            current = self.store.get(str(message["job_id"]))
            if (
                current is not None
                and current["state"] in {"cancelled", "unknown"}
                and current.get("result_json")
            ):
                cancelled_result = json.loads(str(current["result_json"]))
                _emit(output, cancelled_result)
                return 1

            agent_result: dict[str, Any] | None = None
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    candidate = parse_message(json.loads(line))
                except (json.JSONDecodeError, ContractError) as exc:
                    raise RunnerError("Agent emitted an invalid protocol message.", code="runner_agent_protocol_invalid") from exc
                if candidate["job_id"] != message["job_id"] or candidate["request_id"] != message["request_id"]:
                    raise RunnerError("Agent response identity does not match the job.", code="runner_agent_protocol_invalid")
                if candidate["kind"] == "event":
                    event = self.store.add_event(
                        str(message["job_id"]),
                        str(candidate["category"]),
                        str(candidate.get("message") or ""),
                    )
                    _emit(
                        output,
                        _execution_message(
                            "event",
                            str(message["request_id"]),
                            job_id=str(message["job_id"]),
                            sequence=event["sequence"],
                            category=event["category"],
                            message=event["message"],
                        ),
                    )
                elif candidate["kind"] == "result":
                    if candidate.get("agent") != identity:
                        raise RunnerError("Agent result identity does not match the invocation.", code="runner_agent_identity_mismatch")
                    agent_result = candidate
                else:
                    raise RunnerError("Agent emitted an unsupported response kind.", code="runner_agent_protocol_invalid")
            if agent_result is None:
                message_text = (stderr or "External agent returned no result.").strip()[:4096]
                error = {
                    "code": "agent_process_failed",
                    "message": message_text,
                    "retryable": False,
                }
                agent_result = self._result_message(job, state="failed", body={"ok": False}, error=error)
            state = str(agent_result["state"])
            error_value = agent_result.get("error") or {}
            self.store.finish(
                job["job_id"],
                state=state,
                result=agent_result,
                error_code=str(error_value.get("code") or "") or None,
            )
            _emit(output, agent_result)
            return 0 if state == "succeeded" else 1
        except RunnerError as exc:
            state = "unknown" if effect_mode == "workspace-write" else "failed"
            error = {"code": exc.code, "message": str(exc)[:4096], "retryable": exc.retryable}
            result = self._result_message(job, state=state, body={"ok": False}, error=error)
            self.store.finish(job["job_id"], state=state, result=result, error_code=exc.code)
            _emit(output, result)
            return 1
        finally:
            if temp is not None:
                temp.cleanup()

    def terminate_active(self) -> None:
        self._cancel_requested.set()
        with self._active_lock:
            proc = self._active
        if proc is not None:
            _terminate(proc)
