from __future__ import annotations

import json
import queue
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

from baldr_router import codex_sdk
from baldr_router.codex_app_server import CodexAppServerSession


def _wire_report() -> dict[str, Any]:
    return {
        "status": "implemented",
        "summary": "done",
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": [{"key": "database", "value": "postgresql"}],
        "constraints": [],
        "assumptions": [],
        "alternatives_rejected": [],
        "acceptance_criteria": [],
        "blockers": [],
        "review_decision": "not_applicable",
    }


def test_app_server_normalizes_wire_decisions_before_validation(tmp_path: Path) -> None:
    session = object.__new__(CodexAppServerSession)
    session._notifications = queue.Queue()
    session._stderr = []

    def fake_request(
        self: CodexAppServerSession,
        method: str,
        params: dict[str, Any],
        *,
        timeout: int,
    ) -> dict[str, Any]:
        assert method == "turn/start"
        assert timeout == 60
        self._notifications.put(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(_wire_report()),
                    }
                },
            }
        )
        self._notifications.put({"method": "turn/completed", "params": {}})
        return {}

    session.request = MethodType(fake_request, session)

    result = session.run_turn(
        thread_id="thread-test",
        prompt="test",
        cwd=tmp_path,
        sandbox="read-only",
        model="",
        timeout=1,
        report_kind="implementation",
    )

    assert result["ok"] is True
    assert result["final_report"]["decisions"] == {"database": "postgresql"}


def test_sdk_normalizes_wire_decisions_before_validation(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeThread:
        id = "thread-test"

        def run(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(final_response=json.dumps(_wire_report()))

    class FakeCodex:
        def thread_start(self, **kwargs: Any) -> FakeThread:
            return FakeThread()

    monkeypatch.setattr(codex_sdk, "_THREADS", {})
    monkeypatch.setattr(codex_sdk, "_ensure_codex", lambda: FakeCodex())
    monkeypatch.setattr(codex_sdk, "_sandbox_value", lambda value: value)

    result = codex_sdk.run_codex_sdk(
        prompt="test",
        cwd=tmp_path,
        model="",
        sandbox="read-only",
        timeout=1,
        session_scope="workspace",
        session_key="normalization-test",
        telemetry_enabled=False,
        report_kind="implementation",
    )

    assert result["ok"] is True
    assert result["final_report"]["decisions"] == {"database": "postgresql"}
