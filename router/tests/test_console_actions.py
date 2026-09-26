"""The console's one write surface.

Every test here exists to prove the same property: the page proposes, and the
router disposes. Nothing the browser sends decides what happens to durable
state.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from baldr_router.console_service import (
    CONSOLE_ACTIONS,
    TOKEN_HEADER,
    build_console_server,
    serve_console_in_background,
)
from baldr_router.facade import facade_run

TOKEN = "action-test-token"


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
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(tmp_path)])
    )
    return repo


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


def _post(
    console: str,
    body: object,
    *,
    token: str | None = TOKEN,
    content_type: str = "application/json",
) -> tuple[int, dict]:
    headers = {"Content-Type": content_type}
    if token is not None:
        headers[TOKEN_HEADER] = token
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{console}/v1/actions", data=raw, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _draft_item(workspace: Path) -> str:
    result = facade_run(
        str(workspace),
        "console action fixture",
        client="test",
        work_item_action="create-item",
        title="Fixture",
    )
    return str(result["work_item"]["id"])


def test_an_action_the_item_does_not_allow_is_refused(
    console: str, workspace: Path
) -> None:
    """A draft has nothing to reconcile, so the router must say no."""

    item_id = _draft_item(workspace)

    status, payload = _post(console, {"work_item_id": item_id, "action": "cancel"})

    assert status == 409
    assert payload["error"]["code"] == "action_not_allowed"


def test_an_unknown_action_is_refused(console: str, workspace: Path) -> None:
    item_id = _draft_item(workspace)

    status, payload = _post(
        console, {"work_item_id": item_id, "action": "rm -rf /"}
    )

    assert status == 400
    assert payload["error"]["code"] == "action_not_supported"


def test_creating_work_is_not_a_console_action() -> None:
    """Creating and continuing need the composer, so they are not reachable."""

    for action in ("start", "continue", "delete", "archive", "restore", "execute"):
        assert action not in CONSOLE_ACTIONS
    assert "cancel" in CONSOLE_ACTIONS
    assert "mark_failed" in CONSOLE_ACTIONS


def test_an_unknown_item_is_not_found(console: str) -> None:
    status, payload = _post(
        console, {"work_item_id": "wi-does-not-exist", "action": "cancel"}
    )

    assert status == 404
    assert payload["error"]["code"] == "work_item_not_found"


def test_actions_require_the_session_token(console: str, workspace: Path) -> None:
    item_id = _draft_item(workspace)

    status, payload = _post(
        console, {"work_item_id": item_id, "action": "cancel"}, token=None
    )

    assert status == 401
    assert payload["error"]["code"] == "console_token_required"


def test_a_form_content_type_is_refused(console: str, workspace: Path) -> None:
    """A cross-site form cannot send JSON, so JSON is required."""

    item_id = _draft_item(workspace)

    status, payload = _post(
        console,
        {"work_item_id": item_id, "action": "cancel"},
        content_type="application/x-www-form-urlencoded",
    )

    assert status == 415
    assert payload["error"]["code"] == "json_required"


def test_an_oversized_body_is_refused(console: str) -> None:
    status, payload = _post(console, b'{"work_item_id": "' + b"x" * 8192 + b'"}')

    assert status == 413
    assert payload["error"]["code"] == "body_too_large"


def test_malformed_json_is_refused(console: str) -> None:
    status, payload = _post(console, b"{not json")

    assert status == 400
    assert payload["error"]["code"] == "invalid_json"


def test_a_missing_field_is_refused(console: str) -> None:
    status, payload = _post(console, {"action": "cancel"})

    assert status == 400
    assert payload["error"]["code"] == "invalid_action"


def test_the_page_only_renders_actions_the_router_allows() -> None:
    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    assert "if (!allowed.includes(action.id)) continue;" in page


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


def test_a_permitted_decision_reaches_durable_state(
    console: str, workspace: Path, monkeypatch
) -> None:
    """The happy path, end to end: a real run is stopped from the page."""

    import threading

    from baldr_router import workflows
    from baldr_router.work_items import WorkItemService

    running = threading.Event()
    release = threading.Event()

    def blocking_provider(**kwargs):
        role = kwargs["role_name"]
        if role == "architect":
            running.set()
            # Hold the run open long enough for the console to act on it.
            release.wait(timeout=60)
        status = {"architect": "planned", "implementer": "implemented"}.get(
            role, "approved"
        )
        return {
            "ok": True,
            "provider": kwargs["provider"],
            "role": role,
            "final_report": _report(status, f"{role} completed"),
        }

    monkeypatch.setattr(workflows, "run_provider_role", blocking_provider)
    item_id = _draft_item(workspace)

    worker = threading.Thread(
        target=lambda: facade_run(
            str(workspace),
            "",
            client="test",
            work_item_action="start-item",
            work_item_id=item_id,
        ),
        daemon=True,
    )
    worker.start()
    assert running.wait(timeout=60), "the fixture run never reached the provider"

    status, payload = _post(console, {"work_item_id": item_id, "action": "cancel"})
    release.set()
    worker.join(timeout=60)

    assert status == 200, payload
    assert payload["action"] == "cancel"
    item = WorkItemService().get(item_id)
    assert item["status"] in {"cancelling", "cancelled"}, item["status"]
