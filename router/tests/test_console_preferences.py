"""The console's configuration surface.

Configuration decides how the next run treats the repository, so the same rule
as every other write applies: the legal values are rebuilt from the router, not
taken from the request.
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
    serve_console_in_background,
)
from baldr_router.work_items import WorkItemService

TOKEN = "preferences-test-token"


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


def _server(workspace_root: str | None) -> Iterator[str]:
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
def console(workspace: Path) -> Iterator[str]:
    yield from _server(str(workspace))


@pytest.fixture
def unscoped_console(workspace: Path) -> Iterator[str]:
    yield from _server(None)


def _post(console: str, body: object, *, token: str | None = TOKEN) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(
        f"{console}/v1/preferences",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def test_a_preference_reaches_durable_state(console: str, workspace: Path) -> None:
    status, payload = _post(console, {"preset": "deep"})

    assert status == 200, payload
    assert payload["preferences"]["preset"] == "deep"
    assert WorkItemService().preferences(str(workspace))["preset"] == "deep"


def test_several_preferences_save_together(console: str, workspace: Path) -> None:
    status, payload = _post(console, {"preset": "fast", "context_mode": "off"})

    assert status == 200, payload
    stored = WorkItemService().preferences(str(workspace))
    assert stored["preset"] == "fast"
    assert stored["context_mode"] == "off"


def test_a_value_the_router_does_not_offer_is_refused(
    console: str, workspace: Path
) -> None:
    status, payload = _post(console, {"preset": "turbo"})

    assert status == 400
    assert payload["error"]["code"] == "invalid_preference"
    assert WorkItemService().preferences(str(workspace))["preset"] != "turbo"


def test_an_unknown_field_is_ignored_and_reported(console: str) -> None:
    """Only the four published preferences exist, so nothing else is a save."""

    status, payload = _post(console, {"durability": "off", "telemetry": "off"})

    assert status == 400
    assert payload["error"]["code"] == "invalid_preference"


def test_dropping_git_protection_needs_consent(console: str, workspace: Path) -> None:
    status, payload = _post(
        console, {"safety_mode": "non-git", "allow_non_git": False}
    )

    assert status == 409
    assert payload["error"]["code"] == "workspace_non_git_confirmation_required"
    assert WorkItemService().preferences(str(workspace))["safety_mode"] != "non-git"


def test_dropping_git_protection_is_allowed_with_consent(
    console: str, workspace: Path
) -> None:
    status, payload = _post(console, {"safety_mode": "non-git", "allow_non_git": True})

    assert status == 200, payload
    assert WorkItemService().preferences(str(workspace))["safety_mode"] == "non-git"


def test_preferences_require_the_session_token(console: str) -> None:
    status, payload = _post(console, {"preset": "deep"}, token=None)

    assert status == 401
    assert payload["error"]["code"] == "console_token_required"


def test_an_unscoped_console_cannot_configure(unscoped_console: str) -> None:
    """Preferences belong to one workspace, so the scope has to be explicit."""

    status, payload = _post(unscoped_console, {"preset": "deep"})

    assert status == 409
    assert payload["error"]["code"] == "workspace_scope_required"


def test_trust_and_secrets_are_not_configurable_from_the_page() -> None:
    from baldr_router.console_service import _PREFERENCE_FIELDS

    assert set(_PREFERENCE_FIELDS) == {
        "safety_mode",
        "preset",
        "context_mode",
        "team_mode",
    }
    for forbidden in ("trusted", "api_key", "context7_api_key", "provider", "token"):
        assert forbidden not in _PREFERENCE_FIELDS


def test_the_page_offers_only_the_published_options() -> None:
    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    # The groups are driven by the router's own option lists, never hardcoded.
    for option_key in ("safety_modes", "presets", "context_modes", "team_modes"):
        assert f"'{option_key}'" in page
    assert "options[optionKey] || []" in page


# --- which agent configuration covers each phase -----------------------------


@pytest.fixture
def two_profiles(workspace: Path) -> list[str]:
    """A second execution profile, so choosing between them means something."""

    from baldr_router.work_items import upsert_execution_profile

    upsert_execution_profile(
        "reviewer-kiro", provider="kiro-cli", agent="kiro_default", description="Kiro"
    )
    return ["default", "reviewer-kiro"]


def test_each_phase_can_be_given_its_own_profile(
    console: str, workspace: Path, two_profiles: list[str]
) -> None:
    status, payload = _post(
        console,
        {
            "role_profiles": {
                "architect": ["default"],
                "implementer": ["default"],
                "reviewer": ["reviewer-kiro", "default"],
            }
        },
    )

    assert status == 200, payload
    stored = WorkItemService().preferences(str(workspace))["role_profiles"]
    assert stored["reviewer"] == ["reviewer-kiro", "default"]
    assert stored["architect"] == ["default"]


def test_the_order_is_the_fallback_order(
    console: str, workspace: Path, two_profiles: list[str]
) -> None:
    """A phase tries its profiles in order, so the first one is the primary."""

    _post(
        console,
        {
            "role_profiles": {
                "architect": ["reviewer-kiro", "default"],
                "implementer": ["default"],
                "reviewer": ["default"],
            }
        },
    )

    stored = WorkItemService().preferences(str(workspace))["role_profiles"]
    assert stored["architect"][0] == "reviewer-kiro"


def test_a_profile_the_router_does_not_define_is_refused(
    console: str, workspace: Path
) -> None:
    status, payload = _post(
        console,
        {
            "role_profiles": {
                "architect": ["gpt-9-ultra"],
                "implementer": ["default"],
                "reviewer": ["default"],
            }
        },
    )

    assert status == 400
    assert payload["error"]["code"] == "invalid_role_profiles"
    stored = WorkItemService().preferences(str(workspace))["role_profiles"]
    assert "gpt-9-ultra" not in stored.get("architect", [])


def test_a_partial_map_is_refused(console: str, workspace: Path) -> None:
    """Saving replaces the whole map, so a partial one would empty a phase."""

    status, payload = _post(console, {"role_profiles": {"architect": ["default"]}})

    assert status == 400
    assert payload["error"]["code"] == "invalid_role_profiles"
    stored = WorkItemService().preferences(str(workspace))["role_profiles"]
    assert set(stored) == {"architect", "implementer", "reviewer"}


def test_a_phase_with_no_profile_is_refused(console: str) -> None:
    status, payload = _post(
        console,
        {
            "role_profiles": {
                "architect": [],
                "implementer": ["default"],
                "reviewer": ["default"],
            }
        },
    )

    assert status == 400
    assert payload["error"]["code"] == "invalid_role_profiles"


def test_an_invented_phase_is_refused(console: str) -> None:
    status, payload = _post(
        console,
        {
            "role_profiles": {
                "architect": ["default"],
                "implementer": ["default"],
                "reviewer": ["default"],
                "deployer": ["default"],
            }
        },
    )

    assert status == 400
    assert payload["error"]["code"] == "invalid_role_profiles"


def test_the_phase_team_saves_without_any_other_preference(
    console: str, workspace: Path
) -> None:
    """It is a save in its own right, not a rider on a mode change."""

    status, payload = _post(
        console,
        {
            "role_profiles": {
                "architect": ["default"],
                "implementer": ["default"],
                "reviewer": ["default"],
            }
        },
    )

    assert status == 200, payload
    assert payload["preferences"]["role_profiles"]["architect"] == ["default"]


# --- whether the documentation setting can do anything -----------------------


def _workbench(console: str) -> dict:
    request = urllib.request.Request(
        f"{console}/v1/workbench", headers={TOKEN_HEADER: TOKEN}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return dict(json.loads(response.read()))


def test_the_view_reports_whether_context7_can_contribute(console: str) -> None:
    """A mode can be selected and still change nothing, so the state travels."""

    state = _workbench(console)["context7"]

    assert state["api_key_available"] is False
    assert state["api_key_source"] == "env:CONTEXT7_API_KEY"
    assert isinstance(state["enabled"], bool)


def test_the_context7_state_never_carries_the_key(
    console: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7-super-secret-value")

    payload = _workbench(console)

    assert payload["context7"]["api_key_available"] is True
    # The source is the name of where the key lives, never the key.
    assert "ctx7-super-secret-value" not in json.dumps(payload)


def test_the_page_warns_that_an_active_helper_without_a_key_does_nothing() -> None:
    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    assert "falta la key" in page
    assert "api_key_available" in page
    # Keys are never collected by the page, only reported as present or not.
    assert "setup-context7" in page


def test_the_page_says_when_the_preset_overrides_the_phase_team() -> None:
    """Only the custom preset respects per-profile effort, so the page says so."""

    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    assert "preset !== 'custom'" in page
    assert "A medida" in page
