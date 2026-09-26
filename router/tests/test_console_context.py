"""Pointing a task at files from the page.

An attachment is a pointer, never an upload: the task records a path and the
agent reads it inside the workspace it was already authorized for. So the rules
worth pinning are about which paths may be named, and these tests hold the line
in both directions — the listing must not offer a secret, and the gate must not
accept one that was never offered.
"""

from __future__ import annotations

import json
import subprocess
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
from baldr_router.discovery.inventory import (
    attachment_record,
    resolve_attachment,
    workspace_listing,
)
from baldr_router.work_items import WorkItemService

TOKEN = "context-test-token"


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", *args],
        check=True,
        capture_output=True,
    )


def _repo(path: Path) -> Path:
    """A repository shaped like a real one: sources, secrets and build output."""

    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text("build/\nnode_modules/\n", encoding="utf-8")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (path / "src" / "util.py").write_text("X = 1\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
    # Never offered and never attachable, by pattern rather than by listing.
    (path / ".env").write_text("TOKEN=hunter2\n", encoding="utf-8")
    (path / "deploy.key").write_text("-----BEGIN-----\n", encoding="utf-8")
    # Ignored by Git, and excluded by name when Git is unavailable.
    (path / "build").mkdir()
    (path / "build" / "out.js").write_text("//\n", encoding="utf-8")
    (path / "node_modules" / "left-pad").mkdir(parents=True)
    (path / "node_modules" / "left-pad" / "index.js").write_text("//\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(tmp_path)])
    )
    return _repo(tmp_path / "repo")


@pytest.fixture
def synthetic_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep compose from reaching a real provider; the record is what matters."""

    def provider(**kwargs: object) -> dict[str, object]:
        role = str(kwargs["role_name"])
        status = {"architect": "planned", "implementer": "implemented"}.get(
            role, "approved"
        )
        return {
            "ok": True,
            "provider": kwargs["provider"],
            "role": role,
            "final_report": {
                "status": status,
                "summary": f"{role} completed",
                "files_modified": [],
                "commands_run": [],
                "tests_run": [],
                "verification_needed": [],
                "risks": [],
                "follow_up": [],
                "decisions": {"write_authorization": "not_required"},
            },
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


def _get(console: str, path: str, *, token: str | None = TOKEN) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(f"{console}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _compose(console: str, body: object) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{console}/v1/compose",
        data=json.dumps(body).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _paths(payload: dict, kind: str | None = None) -> set[str]:
    return {
        str(entry["path"])
        for entry in payload.get("entries", [])
        if kind is None or entry.get("kind") == kind
    }


# --- what the listing offers -------------------------------------------------


def test_the_listing_offers_files_and_the_directories_holding_them(
    console: str,
) -> None:
    status, payload = _get(console, "/v1/context?workspace_root=")

    assert status == 200, payload
    assert payload["source"] == "git-ls-files"
    assert {"README.md", "src/app.py", "docs/design.md"} <= _paths(payload, "file")
    # A directory is attachable in its own right, and says how much it holds.
    directories = {
        entry["path"]: entry["file_count"]
        for entry in payload["entries"]
        if entry["kind"] == "directory"
    }
    assert directories["src"] == 2
    assert directories["docs"] == 1


def test_the_listing_never_offers_a_secret(console: str) -> None:
    _, payload = _get(console, "/v1/context?workspace_root=")
    offered = _paths(payload)

    assert ".env" not in offered
    assert "deploy.key" not in offered


def test_the_listing_respects_gitignore(console: str) -> None:
    """Build output is noise, and .gitignore already says which files are."""

    _, payload = _get(console, "/v1/context?workspace_root=")
    offered = _paths(payload)

    assert payload["gitignore_respected"] is True
    assert "build/out.js" not in offered
    assert "node_modules/left-pad/index.js" not in offered


def test_the_listing_needs_the_session_token(console: str) -> None:
    status, payload = _get(console, "/v1/context?workspace_root=", token=None)

    assert status == 401
    assert payload["error"]["code"] == "console_token_required"


def test_the_listing_refuses_a_workspace_it_was_not_opened_for(
    console: str, tmp_path: Path
) -> None:
    other = _repo(tmp_path / "other")

    status, payload = _get(console, f"/v1/context?workspace_root={other}")

    assert status == 409
    assert payload["error"]["code"] == "workspace_locked"


def test_an_untrusted_workspace_is_not_enumerated(tmp_path: Path) -> None:
    """Baldr may watch a workspace it cannot read; listing it is still refused."""

    stranger = _repo(tmp_path / "stranger")
    listing = workspace_listing(stranger)

    assert listing["ok"] is False
    assert listing["entries"] == []


# --- the gate every supplied path passes -------------------------------------


def test_a_listed_path_is_recorded_as_a_pointer_not_content(
    console: str, workspace: Path, synthetic_provider: None
) -> None:
    status, payload = _compose(
        console,
        {"task": "Revisar la app", "attachments": ["src/app.py", "docs"]},
    )

    assert status == 202, payload
    item = WorkItemService().get(payload["work_item_id"])
    recorded = item["config"]["attachments"]
    assert {entry["label"] for entry in recorded} == {"src/app.py", "docs"}
    assert {entry["kind"] for entry in recorded} == {"file", "directory"}
    # The pointer is absolute and inside the workspace; the content is not copied.
    for entry in recorded:
        assert Path(entry["path"]).is_relative_to(workspace)
        assert "print('hi')" not in json.dumps(recorded)


@pytest.mark.parametrize(
    "refused",
    [
        "../outside.txt",
        "/etc/passwd",
        "src/../../outside.txt",
        ".env",
        "deploy.key",
        "node_modules/left-pad/index.js",
        "src/does-not-exist.py",
        "~/secrets",
        "C:\\Windows\\win.ini",
    ],
)
def test_a_path_outside_the_rules_is_refused(console: str, refused: str) -> None:
    status, payload = _compose(console, {"task": "algo", "attachments": [refused]})

    assert status == 403, payload
    assert payload["error"]["code"] == "attachment_not_allowed"


def test_one_refused_path_fails_the_whole_request(
    console: str, synthetic_provider: None
) -> None:
    """Running with less context than was attached is worse than not running."""

    status, payload = _compose(
        console, {"task": "algo", "attachments": ["src/app.py", ".env"]}
    )

    assert status == 403
    assert payload["error"]["code"] == "attachment_not_allowed"
    assert WorkItemService().list(workspace_root=None) == []


def test_more_attachments_than_allowed_are_refused(console: str) -> None:
    status, payload = _compose(
        console, {"task": "algo", "attachments": [f"src/app.py#{n}" for n in range(60)]}
    )

    assert status == 400
    assert payload["error"]["code"] == "too_many_attachments"


def test_a_file_created_after_the_listing_is_still_attachable(
    console: str, workspace: Path, synthetic_provider: None
) -> None:
    """The listing is discovery; the gate is the authority.

    Pinning this keeps the two from being collapsed into one cached answer,
    where a file written a second ago would be unattachable until a cache
    expired, and a stale entry would be trusted because it was once listed.
    """
    listed, payload = _get(console, "/v1/context?workspace_root=")
    assert listed == 200
    assert "src/brand_new.py" not in _paths(payload)

    (workspace / "src" / "brand_new.py").write_text("Y = 2\n", encoding="utf-8")

    status, composed = _compose(
        console, {"task": "Mirar lo nuevo", "attachments": ["src/brand_new.py"]}
    )

    assert status == 202, composed
    item = WorkItemService().get(composed["work_item_id"])
    assert item["config"]["attachments"][0]["label"] == "src/brand_new.py"


def test_a_task_with_no_attachments_is_unchanged(
    console: str, synthetic_provider: None
) -> None:
    status, payload = _compose(console, {"task": "Sin contexto adjunto"})

    assert status == 202, payload
    item = WorkItemService().get(payload["work_item_id"])
    assert item["config"]["attachments"] == []


# --- the rules themselves, without a server in the way -----------------------


def test_the_gate_accepts_a_path_the_listing_would_offer(workspace: Path) -> None:
    resolved = resolve_attachment(workspace, "src/app.py")

    assert resolved == (workspace / "src" / "app.py").resolve()


def test_the_gate_refuses_a_symlink_that_leaves_the_workspace(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    link = workspace / "src" / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks here")

    assert resolve_attachment(workspace, "src/escape.txt") is None


def test_the_gate_accepts_a_symlink_that_stays_inside(workspace: Path) -> None:
    """Containment is the invariant, not the absence of symlinks.

    A repository that links one of its own files is ordinary, and the path it
    resolves to is one the agent was already authorized to read.
    """
    link = workspace / "shortcut.py"
    try:
        link.symlink_to(workspace / "src" / "app.py")
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks here")

    record = attachment_record(workspace, "shortcut.py")

    assert record is not None
    assert record["label"] == "src/app.py"


def test_the_record_names_the_path_relative_to_the_workspace(workspace: Path) -> None:
    record = attachment_record(workspace, "docs/design.md")

    assert record == {
        "kind": "file",
        "label": "docs/design.md",
        "path": str((workspace / "docs" / "design.md").resolve()),
    }


def test_the_page_keeps_attached_paths_across_refreshes() -> None:
    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    # The selection outlives its DOM for the same reason an unsent draft does.
    assert "const attachments = new Map();" in page
    assert "attachments.delete(draftKey);" in page
