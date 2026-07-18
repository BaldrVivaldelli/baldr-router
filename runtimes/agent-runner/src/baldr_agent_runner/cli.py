from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from baldr_agent_sdk.contract import ContractError, canonical_json

from .runner import LocalAgentRunner, RunnerError, _execution_message
from .store import RunnerStore


def _error(
    request_id: str,
    job_id: str,
    exc: Exception,
    *,
    agent: Any = None,
) -> dict[str, Any]:
    code = exc.code if isinstance(exc, RunnerError) else "runner_request_invalid"
    retryable = exc.retryable if isinstance(exc, RunnerError) else False
    identity = (
        dict(agent)
        if isinstance(agent, dict)
        and isinstance(agent.get("ref"), str)
        and isinstance(agent.get("digest"), str)
        else {
            "ref": "local://runner/error@1",
            "digest": "sha256:" + "0" * 64,
        }
    )
    return _execution_message(
        "result",
        request_id or "runner-error",
        job_id=job_id or "runner-error",
        state="failed",
        agent=identity,
        result={"ok": False},
        error={"code": code, "message": str(exc)[:4096], "retryable": retryable},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baldr-agent-runner")
    parser.add_argument("command", choices=["stdio", "health"])
    parser.add_argument("--state", type=Path)
    args = parser.parse_args(argv)
    if args.command == "health":
        sys.stdout.write(
            canonical_json(
                _execution_message(
                    "health-response",
                    "runner-health",
                    status="ok",
                    runner_version="0.20.0",
                    protocols=[1],
                )
            )
            + "\n"
        )
        sys.stdout.flush()
        return 0
    raw = sys.stdin.readline(2_100_000)
    if not raw:
        print("Expected one execution request on stdin.", file=sys.stderr)
        return 2
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        print("Execution request is not valid JSON.", file=sys.stderr)
        return 2
    runner = LocalAgentRunner(store=RunnerStore(args.state))

    def terminate(signum: int, frame: Any) -> None:
        del signum, frame
        runner.terminate_active()

    if args.command == "stdio":
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
    target: dict[str, Any] | None = None
    raw_target = os.environ.get("BALDR_AGENT_TARGET_JSON", "")
    if raw_target:
        try:
            value = json.loads(raw_target)
            target = value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            target = None
    try:
        return runner.handle(request, output=sys.stdout, target=target)
    except (ContractError, RunnerError, OSError, ValueError) as exc:
        request_id = str(request.get("request_id") or "runner-error") if isinstance(request, dict) else "runner-error"
        job_id = str(request.get("job_id") or "runner-error") if isinstance(request, dict) else "runner-error"
        agent = request.get("agent") if isinstance(request, dict) else None
        sys.stdout.write(
            canonical_json(_error(request_id, job_id, exc, agent=agent)) + "\n"
        )
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
