from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from baldr_router import cli, codex


def _configure_catalog_session(monkeypatch, session_type: type[Any]) -> None:
    codex.reset_codex_model_catalog_cache()
    monkeypatch.setattr(codex, "codex_found", lambda: "/usr/bin/codex")
    monkeypatch.setattr(codex, "_codex_env", lambda extra_env=None: {})
    monkeypatch.setattr(codex, "CodexAppServerSession", session_type)


class FakeModelSession:
    created = 0
    closed = 0

    def __init__(self, **_: Any) -> None:
        type(self).created += 1

    def request(
        self, method: str, params: dict[str, Any], timeout: int
    ) -> dict[str, Any]:
        assert method == "model/list"
        assert params == {"limit": 100, "includeHidden": False}
        assert timeout == 30
        return {
            "data": [
                {
                    "id": "catalog-entry-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "description": "Frontier model",
                    "hidden": False,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Fast"},
                        {"reasoningEffort": "high", "description": "Deep"},
                    ],
                    "inputModalities": ["text", "image"],
                    "isDefault": True,
                },
                {
                    "id": "hidden-model",
                    "model": "hidden-model",
                    "displayName": "Hidden",
                    "hidden": True,
                },
            ],
            "nextCursor": None,
        }

    def close(self) -> None:
        type(self).closed += 1


def test_codex_model_catalog_normalizes_and_caches(monkeypatch) -> None:
    FakeModelSession.created = 0
    FakeModelSession.closed = 0
    _configure_catalog_session(monkeypatch, FakeModelSession)

    first = codex.codex_model_catalog()
    assert first["ok"] is True
    assert first["source"] == "codex-app-server"
    assert first["models"] == [
        {
            "id": "catalog-entry-sol",
            "model": "gpt-5.6-sol",
            "display_name": "GPT-5.6-Sol",
            "description": "Frontier model",
            "default_reasoning_effort": "medium",
            "reasoning_efforts": [
                {"id": "low", "description": "Fast"},
                {"id": "high", "description": "Deep"},
            ],
            "is_default": True,
            "input_modalities": ["text", "image"],
        }
    ]
    first["models"].clear()

    second = codex.codex_model_catalog()
    assert len(second["models"]) == 1
    assert FakeModelSession.created == 1
    assert FakeModelSession.closed == 1


def test_codex_model_catalog_uses_the_resolved_executable(monkeypatch) -> None:
    executable = r"C:\Users\runneradmin\AppData\Roaming\npm\codex.cmd"
    captured: dict[str, Any] = {}

    class CapturingSession:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def request(
            self, method: str, params: dict[str, Any], timeout: int
        ) -> dict[str, Any]:
            assert method == "model/list"
            assert params == {"limit": 100, "includeHidden": False}
            assert timeout == 30
            return {"data": [{"id": "gpt-test", "model": "gpt-test"}]}

        def close(self) -> None:
            pass

    codex.reset_codex_model_catalog_cache()
    monkeypatch.setattr(codex, "codex_found", lambda: executable)
    monkeypatch.setattr(codex, "_codex_env", lambda extra_env=None: {})
    monkeypatch.setattr(codex, "CodexAppServerSession", CapturingSession)

    result = codex.codex_model_catalog(force=True)

    assert result["ok"] is True
    assert captured["codex_executable"] == executable


def test_app_server_runner_uses_the_resolved_executable(
    tmp_path: Path, monkeypatch
) -> None:
    executable = r"C:\Users\runneradmin\AppData\Roaming\npm\codex.cmd"
    captured: dict[str, Any] = {}
    config = SimpleNamespace(
        codex=SimpleNamespace(
            runner="app-server",
            model="",
            reasoning_effort="",
            session_scope="workspace",
            timeout_seconds=60,
        ),
        telemetry=SimpleNamespace(enabled=False),
    )

    def fake_run_codex_app_server(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(codex, "load_config", lambda: config)
    monkeypatch.setattr(codex, "codex_preflight", lambda: {"ok": True})
    monkeypatch.setattr(codex, "_codex_env", lambda extra_env=None: {})
    monkeypatch.setattr(codex, "codex_found", lambda: executable)
    monkeypatch.setattr(codex, "run_codex_app_server", fake_run_codex_app_server)

    result = codex._run_codex_prompt(
        cwd=tmp_path,
        prompt="inspect the workspace",
        sandbox="read-only",
        approval_policy="never",
        report_kind="plan",
        runner="app-server",
    )

    assert result["ok"] is True
    assert captured["codex_executable"] == executable


def test_codex_model_catalog_follows_pagination_and_deduplicates_by_model(
    monkeypatch,
) -> None:
    class PaginatedSession:
        requests: list[dict[str, Any]] = []
        closed = 0

        def __init__(self, **_: Any) -> None:
            pass

        def request(
            self, method: str, params: dict[str, Any], timeout: int
        ) -> dict[str, Any]:
            assert method == "model/list"
            assert timeout == 30
            type(self).requests.append(dict(params))
            if "cursor" not in params:
                return {
                    "data": [
                        {
                            "id": "catalog-sol",
                            "model": "gpt-5.6-sol",
                            "displayName": "Sol",
                        }
                    ],
                    "nextCursor": "page-2",
                }
            assert params["cursor"] == "page-2"
            return {
                "data": [
                    {
                        "id": "duplicate-catalog-sol",
                        "model": "gpt-5.6-sol",
                        "displayName": "Duplicate Sol",
                    },
                    {
                        "id": "gpt-5.6-luna",
                        "displayName": "Luna",
                    },
                ],
                "nextCursor": None,
            }

        def close(self) -> None:
            type(self).closed += 1

    _configure_catalog_session(monkeypatch, PaginatedSession)

    result = codex.codex_model_catalog(force=True)

    assert result["ok"] is True
    assert PaginatedSession.requests == [
        {"limit": 100, "includeHidden": False},
        {"limit": 100, "includeHidden": False, "cursor": "page-2"},
    ]
    assert [model["model"] for model in result["models"]] == [
        "gpt-5.6-sol",
        "gpt-5.6-luna",
    ]
    assert result["models"][0]["id"] == "catalog-sol"
    assert result["models"][1]["id"] == "gpt-5.6-luna"
    assert PaginatedSession.closed == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(None, id="not-an-object"),
        pytest.param({}, id="missing-data"),
        pytest.param({"data": {}}, id="data-not-a-list"),
    ],
)
def test_codex_model_catalog_rejects_invalid_responses(monkeypatch, response) -> None:
    class InvalidResponseSession:
        closed = 0

        def __init__(self, **_: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> Any:
            return response

        def close(self) -> None:
            type(self).closed += 1

    _configure_catalog_session(monkeypatch, InvalidResponseSession)

    result = codex.codex_model_catalog(force=True)

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_model_list_invalid_response"
    assert InvalidResponseSession.closed == 1


def test_codex_model_catalog_rejects_repeated_pagination_cursor(monkeypatch) -> None:
    class RepeatedCursorSession:
        requests = 0
        closed = 0

        def __init__(self, **_: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            type(self).requests += 1
            return {
                "data": [
                    {
                        "id": f"catalog-{type(self).requests}",
                        "model": f"model-{type(self).requests}",
                    }
                ],
                "nextCursor": "repeated-cursor",
            }

        def close(self) -> None:
            type(self).closed += 1

    _configure_catalog_session(monkeypatch, RepeatedCursorSession)

    result = codex.codex_model_catalog(force=True)

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_model_list_cursor_loop"
    assert RepeatedCursorSession.requests == 2
    assert RepeatedCursorSession.closed == 1


def test_codex_model_catalog_reports_pagination_limit(monkeypatch) -> None:
    class EndlessPaginationSession:
        requests = 0
        closed = 0

        def __init__(self, **_: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            type(self).requests += 1
            page = type(self).requests
            return {
                "data": [{"id": f"model-{page}", "model": f"model-{page}"}],
                "nextCursor": f"cursor-{page}",
            }

        def close(self) -> None:
            type(self).closed += 1

    _configure_catalog_session(monkeypatch, EndlessPaginationSession)

    result = codex.codex_model_catalog(force=True)

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_model_list_truncated"
    assert EndlessPaginationSession.requests == 10
    assert EndlessPaginationSession.closed == 1


def test_codex_model_catalog_reports_app_server_errors(monkeypatch) -> None:
    class FailingSession:
        closed = 0

        def __init__(self, **_: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("model discovery unavailable")

        def close(self) -> None:
            type(self).closed += 1

    _configure_catalog_session(monkeypatch, FailingSession)

    result = codex.codex_model_catalog(force=True)

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_model_list_failed"
    assert FailingSession.closed == 1


def test_provider_models_cli_accepts_codex_alias_and_forwards_refresh(
    monkeypatch, capsys
) -> None:
    calls: list[bool] = []

    def fake_catalog(*, force: bool = False) -> dict[str, Any]:
        calls.append(force)
        return {"ok": True, "provider": "codex", "models": []}

    monkeypatch.setattr(cli, "codex_model_catalog", fake_catalog)

    exit_code = cli.main(["provider-models", "openai-codex", "--refresh"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls == [True]
    assert payload == {"ok": True, "provider": "codex", "models": []}


def test_provider_models_cli_rejects_unsupported_provider(monkeypatch, capsys) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the Codex catalog must not run for another provider")

    monkeypatch.setattr(cli, "codex_model_catalog", fail_if_called)

    exit_code = cli.main(["provider-models", "kiro-cli"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["provider"] == "kiro-cli"
