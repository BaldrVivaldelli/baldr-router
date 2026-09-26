"""Creating the profiles the phases choose between.

A profile names a provider and a model, which is a smaller thing than granting
a workspace trust or storing a credential — the worst a wrong one does is run a
phase on the wrong model. So it is editable from the page, while trust and keys
stay out. What the page cannot do is invent a provider: the adapters that exist
decide that, here as everywhere else.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from baldr_router import console_service
from baldr_router.config import load_config
from baldr_router.console_service import (
    TOKEN_HEADER,
    build_console_server,
    serve_console_in_background,
)

TOKEN = "profiles-test-token"


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=T", "-c",
         "user.email=t@example.invalid", "commit", "-qm", "initial"],
        check=True,
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(tmp_path)])
    )
    # Listing Codex's models opens an app-server session. The catalog's shape is
    # what matters here, not that a CLI is installed on the machine running this.
    monkeypatch.setattr(
        console_service,
        "codex_model_catalog",
        lambda **_: {
            "ok": True,
            "models": [
                {"id": "gpt-6-astra", "display_name": "GPT-6-Astra", "description": "Frontier."}
            ],
        },
    )
    return _repo(tmp_path / "repo")


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
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _post(console: str, body: object, *, path: str = "/v1/profiles") -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{console}{path}",
        data=json.dumps(body).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.status), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return int(error.code), json.loads(error.read() or b"{}")


def _provider(payload: dict, name: str) -> dict:
    return next(entry for entry in payload["providers"] if entry["id"] == name)


# --- what the page may choose between -----------------------------------------


def test_the_catalog_lists_every_implemented_provider(console: str) -> None:
    status, payload = _get(console, "/v1/providers")

    assert status == 200, payload
    assert {entry["id"] for entry in payload["providers"]} == {
        "claude", "codex", "kiro-cli"
    }


def test_a_provider_says_whether_it_could_actually_run(console: str) -> None:
    """Naming an uninstalled provider is a mistake that surfaces at run time."""

    _, payload = _get(console, "/v1/providers")

    for entry in payload["providers"]:
        assert isinstance(entry["available"], bool)


def test_every_unusable_provider_explains_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted with none of them installed, rather than whatever this machine has.

    Written the other way first, this only ran its interesting branch on a
    machine missing a CLI, so it passed locally and failed everywhere else —
    which is how Codex's status turned out to report "unavailable" with no
    reason at all.
    """
    from baldr_router import claude_cli, kiro_cli, provider_registry

    monkeypatch.setattr(provider_registry, "codex_found", lambda *_, **__: None)
    monkeypatch.setattr(claude_cli, "claude_found", lambda *_, **__: None)
    monkeypatch.setattr(kiro_cli, "kiro_cli_found", lambda *_, **__: None)

    reported = provider_registry.get_provider_registry().status()["providers"]

    assert set(reported) == {"claude", "codex", "kiro-cli"}
    for name, status in reported.items():
        assert status["ok"] is False, name
        assert status.get("reason"), f"{name} is unusable and does not say why"


def test_models_are_suggestions_rather_than_a_closed_list(console: str) -> None:
    """A model that reached a CLI but not this list must still be usable."""

    _, payload = _get(console, "/v1/providers")

    codex = _provider(payload, "codex")
    claude = _provider(payload, "claude")
    assert codex["model_source"] == "enumerated"
    assert [model["id"] for model in codex["models"]] == ["gpt-6-astra"]
    assert claude["model_source"] == "aliases"
    assert {model["id"] for model in claude["models"]} >= {"opus", "sonnet", "haiku"}


def test_a_provider_that_picks_an_agent_says_so(console: str) -> None:
    """Kiro selects a named agent; a form offering only models cannot set it."""

    _, payload = _get(console, "/v1/providers")

    assert _provider(payload, "kiro-cli")["configures"] == "agent"
    assert _provider(payload, "claude")["configures"] == "model"


def test_the_catalog_needs_the_session_token(console: str) -> None:
    status, payload = _get(console, "/v1/providers", token=None)

    assert status == 401
    assert payload["error"]["code"] == "console_token_required"


def test_the_catalog_is_not_built_on_the_polled_view(
    console: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing models opens an app-server session; a poll must not wait on it."""

    calls: list[int] = []
    monkeypatch.setattr(
        console_service,
        "codex_model_catalog",
        lambda **_: (calls.append(1), {"ok": True, "models": []})[1],
    )

    _get(console, "/v1/workbench")
    _get(console, "/v1/workbench")
    assert calls == []

    _get(console, "/v1/providers")
    assert len(calls) == 1


# --- creating one -------------------------------------------------------------


def test_a_profile_is_created_and_becomes_choosable(console: str) -> None:
    status, payload = _post(
        console,
        {
            "name": "reviewer-claude",
            "provider": "claude",
            "model": "sonnet",
            "reasoning_effort": "high",
            "description": "Claude revisa lo que Codex implementó",
        },
    )

    assert status == 200, payload
    stored = load_config().execution_profiles["reviewer-claude"]
    assert (stored.provider, stored.model, stored.reasoning_effort) == (
        "claude", "sonnet", "high"
    )
    # And the phase screen can now offer it.
    _, workbench = _get(console, "/v1/workbench")
    assert "reviewer-claude" in workbench["workbench"]["profiles"]["execution_profiles"]


def test_saving_the_same_name_replaces_it(console: str) -> None:
    _post(console, {"name": "tuned", "provider": "claude", "model": "haiku"})

    status, _ = _post(console, {"name": "tuned", "provider": "claude", "model": "opus"})

    assert status == 200
    assert load_config().execution_profiles["tuned"].model == "opus"


def test_a_provider_with_no_adapter_is_refused(console: str) -> None:
    status, payload = _post(console, {"name": "wishful", "provider": "gpt-9-imaginary"})

    assert status == 400
    assert payload["error"]["code"] == "invalid_profile"
    assert "wishful" not in load_config().execution_profiles


@pytest.mark.parametrize("name", ["", "   ", "bad name!", "x" * 80, "-leading"])
def test_a_name_the_config_cannot_hold_is_refused(console: str, name: str) -> None:
    status, payload = _post(console, {"name": name, "provider": "claude"})

    assert status == 400
    assert payload["error"]["code"] == "invalid_profile"


def test_a_profile_needs_a_provider(console: str) -> None:
    status, payload = _post(console, {"name": "orphan"})

    assert status == 400
    assert payload["error"]["code"] == "invalid_profile"


def test_creating_a_profile_requires_the_session_token(console: str) -> None:
    request = urllib.request.Request(
        f"{console}/v1/profiles",
        data=json.dumps({"name": "x", "provider": "claude"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=30)

    assert caught.value.code == 401


def test_trust_and_credentials_are_still_not_editable_from_the_page() -> None:
    """Widening what the page may configure must not widen it to these."""

    from baldr_router.console_service import console_asset_path

    page = console_asset_path().read_text(encoding="utf-8")

    assert "La confianza del workspace, las claves" in page
    for forbidden in ("trusted_roots", "api_key", "trust-workspace"):
        assert f"'{forbidden}'" not in page
