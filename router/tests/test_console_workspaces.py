"""Choosing a workspace from the page.

Picking among workspaces Baldr already knows is safe; naming a new directory
would be a privilege grant. These tests hold that line.
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
    TOKEN_HEADER,
    build_console_server,
    selectable_workspaces,
    serve_console_in_background,
)
from baldr_router.work_items import WorkItemService

TOKEN = "workspace-test-token"


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
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
def known(tmp_path: Path, monkeypatch) -> Path:
    """A workspace Baldr knows because it already has preferences."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(tmp_path)])
    )
    repo = _repo(tmp_path / "known-repo")
    WorkItemService().set_preferences(str(repo), preset="deep")
    return repo


def _console(workspace_root: str | None) -> Iterator[str]:
    server = build_console_server(
        host="127.0.0.1", port=0, token=TOKEN, workspace_root=workspace_root
    )
    serve_console_in_background(server)
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def console(known: Path) -> Iterator[str]:
    yield from _console(None)


@pytest.fixture
def locked_console(known: Path) -> Iterator[str]:
    yield from _console(str(known))


def _get(console: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{console}{path}", headers={TOKEN_HEADER: TOKEN}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _post(console: str, path: str, body: object) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{console}{path}",
        data=json.dumps(body).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def test_the_view_publishes_the_workspaces_it_will_accept(
    console: str, known: Path
) -> None:
    status, payload = _get(console, "/v1/workbench")

    assert status == 200
    roots = {item["root"] for item in payload["workspaces"]}
    assert str(known) in roots
    assert payload["workspace_locked"] is False
    assert payload["workspace_root"] is None


def test_a_published_workspace_can_be_selected(console: str, known: Path) -> None:
    status, payload = _get(
        console, f"/v1/workbench?workspace_root={known}"
    )

    assert status == 200
    assert payload["workspace_root"] == str(known)
    assert payload["workbench"]["preferences"]["preset"] == "deep"


def test_an_unpublished_path_is_refused(console: str, tmp_path: Path) -> None:
    """The page picks from a list; it cannot name a directory itself."""

    stranger = _repo(tmp_path / "stranger")

    status, payload = _get(console, f"/v1/workbench?workspace_root={stranger}")

    assert status == 403
    assert payload["error"]["code"] == "workspace_not_selectable"


@pytest.mark.parametrize("path", ["/etc", "/", "/root"])
def test_a_sensitive_path_is_refused(console: str, path: str) -> None:
    status, payload = _get(console, f"/v1/workbench?workspace_root={path}")

    assert status == 403
    assert payload["error"]["code"] == "workspace_not_selectable"


def test_writes_are_scoped_to_a_published_workspace(
    console: str, known: Path, tmp_path: Path
) -> None:
    stranger = _repo(tmp_path / "stranger-write")

    refused, payload = _post(
        console,
        "/v1/preferences",
        {"preset": "fast", "workspace_root": str(stranger)},
    )
    accepted, _ = _post(
        console, "/v1/preferences", {"preset": "fast", "workspace_root": str(known)}
    )

    assert refused == 403
    assert payload["error"]["code"] == "workspace_not_selectable"
    assert accepted == 200
    assert WorkItemService().preferences(str(known))["preset"] == "fast"


def test_a_locked_console_hides_the_picker_and_stays_put(
    locked_console: str, known: Path, tmp_path: Path
) -> None:
    """`make up WORKSPACE=...` opens a window for one repository."""

    status, payload = _get(locked_console, "/v1/workbench")
    assert status == 200
    assert payload["workspace_locked"] is True
    assert payload["workspace_root"] == str(known)

    other = _repo(tmp_path / "other")
    WorkItemService().set_preferences(str(other), preset="fast")
    refused, body = _get(locked_console, f"/v1/workbench?workspace_root={other}")

    assert refused == 409
    assert body["error"]["code"] == "workspace_locked"


def test_a_workspace_that_disappeared_is_not_offered(
    known: Path, tmp_path: Path, monkeypatch
) -> None:
    gone = tmp_path / "gone"
    WorkItemService().set_preferences(str(_repo(gone)), preset="fast")
    assert str(gone) in {item["root"] for item in selectable_workspaces()}

    subprocess.run(["rm", "-rf", str(gone)], check=True)

    assert str(gone) not in {item["root"] for item in selectable_workspaces()}


def test_the_listing_says_which_workspaces_can_be_worked_in(known: Path) -> None:
    entry = next(w for w in selectable_workspaces() if w["root"] == str(known))

    assert entry["trusted"] is True
    assert entry["label"] == known.name
