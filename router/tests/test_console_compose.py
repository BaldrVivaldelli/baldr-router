"""Starting and continuing work from the console.

A workflow runs for minutes, so the request that starts one returns as soon as
the item is durable. These tests pin that contract, and the rule that a
follow-up is a turn on the same item rather than a second conversation.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from baldr_router import workflows
from baldr_router.console_service import (
    TOKEN_HEADER,
    build_console_server,
    serve_console_in_background,
)
from baldr_router.work_items import WorkItemService

TOKEN = "compose-test-token"


def _report(status: str, summary: str) -> dict:
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"write_authorization": "not_required"},
    }


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(tmp_path)])
    )
    return _repo(tmp_path / "repo")


@pytest.fixture
def synthetic_provider(monkeypatch) -> None:
    def provider(**kwargs):
        role = kwargs["role_name"]
        status = {"architect": "planned", "implementer": "implemented"}.get(
            role, "approved"
        )
        return {
            "ok": True,
            "provider": kwargs["provider"],
            "role": role,
            "final_report": _report(status, f"{role} completed"),
        }

    monkeypatch.setattr(workflows, "run_provider_role", provider)


@pytest.fixture
def console(workspace: Path) -> Iterator[str]:
    server = build_console_server(
        host="127.0.0.1", port=0, token=TOKEN, workspace_root=str(workspace)
    )
    serve_console_in_background(server)
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def unscoped_console(workspace: Path) -> Iterator[str]:
    server = build_console_server(host="127.0.0.1", port=0, token=TOKEN)
    serve_console_in_background(server)
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _compose(console: str, body: object, *, token: str | None = TOKEN) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(
        f"{console}/v1/compose",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _settled(item_id: str, *, timeout: float = 60.0) -> dict:
    deadline = threading.Event()
    service = WorkItemService()
    for _ in range(int(timeout * 10)):
        item = service.get(item_id)
        if item["status"] not in {"running", "draft", "ready"}:
            return item
        deadline.wait(0.1)
    raise AssertionError(f"work item {item_id} never settled")


def test_composing_starts_work_and_returns_before_it_finishes(
    console: str, synthetic_provider: None
) -> None:
    status, payload = _compose(console, {"task": "Escribir una nota"})

    # 202: the item is durable, the workflow has only begun.
    assert status == 202, payload
    assert payload["started"] is True
    item_id = payload["work_item_id"]
    assert WorkItemService().get(item_id)["id"] == item_id
    assert _settled(item_id)["status"] == "completed"


def test_a_follow_up_is_a_turn_on_the_same_item(
    console: str, synthetic_provider: None
) -> None:
    """Continuing must not open a second conversation."""

    _, first = _compose(console, {"task": "Primera tarea"})
    item_id = first["work_item_id"]
    _settled(item_id)

    status, second = _compose(
        console, {"task": "Ahora corregí el título", "work_item_id": item_id}
    )
    assert status == 202, second
    assert second["work_item_id"] == item_id
    _settled(item_id)

    service = WorkItemService()
    assert len(service.get(item_id)["turns"]) >= 2
    assert len(service.list(workspace_root=None)) == 1


def test_composing_uses_the_configured_preferences(
    console: str, workspace: Path, synthetic_provider: None
) -> None:
    """The settings tab decides how new work runs, not the compose request."""

    WorkItemService().set_preferences(str(workspace), preset="deep")

    _, payload = _compose(console, {"task": "Tarea con preferencias"})

    item = WorkItemService().get(payload["work_item_id"])
    assert item["preset"] == "deep"


def test_an_empty_task_is_refused(console: str) -> None:
    status, payload = _compose(console, {"task": "   "})

    assert status == 400
    assert payload["error"]["code"] == "task_required"


def test_an_oversized_task_is_refused(console: str) -> None:
    status, payload = _compose(console, {"task": "x" * 20_000})

    assert status in {413, 429}
    assert payload["error"]["code"] in {"body_too_large", "too_many_starts"}


def test_composing_requires_the_session_token(console: str) -> None:
    status, payload = _compose(console, {"task": "algo"}, token=None)

    assert status == 401
    assert payload["error"]["code"] == "console_token_required"


def test_an_unscoped_console_cannot_create_work(unscoped_console: str) -> None:
    status, payload = _compose(unscoped_console, {"task": "algo"})

    assert status == 409
    assert payload["error"]["code"] == "workspace_scope_required"


def test_continuing_an_unknown_item_is_refused(console: str) -> None:
    status, payload = _compose(
        console, {"task": "algo", "work_item_id": "wi-nope"}
    )

    assert status in {400, 404}
    assert payload["ok"] is False


def test_the_page_keeps_an_unsent_draft_across_refreshes() -> None:
    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    assert "drafts.get(draftKey)" in page
    assert "drafts.set(draftKey, textarea.value)" in page
