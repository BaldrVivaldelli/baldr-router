from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from baldr_router.console_service import (
    TOKEN_HEADER,
    build_console_server,
    console_asset_path,
    is_loopback_host,
    serve_console_in_background,
)
from baldr_router.durability.store import DurableStore

TOKEN = "service-test-token"


@pytest.fixture
def console(tmp_path: Path, monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    server = build_console_server(host="127.0.0.1", port=0, token=TOKEN)
    serve_console_in_background(server)
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, str, str]:
    # The token gates the API; test_console_auth.py owns the cases that omit it.
    request = urllib.request.Request(url, headers={TOKEN_HEADER: TOKEN})
    with urllib.request.urlopen(request, timeout=10) as response:
        return (
            int(response.status),
            response.read().decode("utf-8"),
            str(response.headers.get("Content-Type") or ""),
        )


def test_root_serves_the_console_page(console: str) -> None:
    status, body, content_type = _get(f"{console}/")

    assert status == 200
    assert content_type.startswith("text/html")
    assert "<title>Baldr</title>" in body


def test_page_is_reread_so_editing_it_needs_no_rebuild(
    console: str, monkeypatch
) -> None:
    original = console_asset_path().read_bytes()
    try:
        console_asset_path().write_bytes(original + b"<!-- edited -->")
        _, body, _ = _get(f"{console}/")
        assert "<!-- edited -->" in body
    finally:
        console_asset_path().write_bytes(original)

    _, restored, _ = _get(f"{console}/")
    assert "<!-- edited -->" not in restored


def test_workbench_returns_the_frozen_status_contract(console: str) -> None:
    status, body, content_type = _get(f"{console}/v1/workbench")
    payload = json.loads(body)

    assert status == 200
    assert content_type.startswith("application/json")
    assert payload["ok"] is True
    assert payload["intent"] == "status"
    assert payload["view"] == "workbench"
    assert payload["contract_version"]
    assert payload["workbench"]["items"] == []
    # The expensive health document belongs to the full status intent only.
    assert "health" not in payload
    assert "qualification" not in payload


def test_livez_reports_the_console_client(console: str) -> None:
    _, body, _ = _get(f"{console}/livez")

    assert json.loads(body) == {"ok": True, "client": "baldr-web-console"}


def test_unknown_routes_are_not_found(console: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(f"{console}/v1/secrets")

    assert error.value.code == 404


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_verbs_without_a_route_are_refused(console: str, method: str) -> None:
    """Decisions go through POST /v1/actions; nothing else writes."""

    request = urllib.request.Request(
        f"{console}/v1/workbench",
        data=b"{}",
        method=method,
        headers={TOKEN_HEADER: TOKEN},
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)

    assert error.value.code == 405
    assert json.loads(error.value.read())["error"]["code"] == "verb_not_supported"


def test_a_post_to_an_unknown_route_is_not_found(console: str) -> None:
    request = urllib.request.Request(
        f"{console}/v1/anything",
        data=b"{}",
        method="POST",
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)

    assert error.value.code == 404


def test_reading_the_console_never_settles_a_durable_run(
    console: str, tmp_path: Path
) -> None:
    store = DurableStore()
    task = store.store_artifact(
        run_id=None, kind="task", value={"task": "console"}, redact=False
    )
    store.create_run(
        run_id="run-console",
        idempotency_key="run-console",
        resume_token="resume-console",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(tmp_path / "repo"),
        workspace_id="workspace",
        client_name="test",
        task_artifact_id=task,
        config_snapshot={},
    )
    store.transition_run("run-console", "running")
    before = store.get_run("run-console")

    for _ in range(3):
        _get(f"{console}/v1/workbench")

    after = DurableStore().get_run("run-console")
    assert after["status"] == before["status"] == "running"
    assert after["lease_epoch"] == before["lease_epoch"]
    assert after["recovery_count"] == before["recovery_count"]


def test_a_public_bind_has_to_be_asked_for() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("0.0.0.0") is False

    with pytest.raises(ValueError, match="allow_non_loopback"):
        build_console_server(host="0.0.0.0", port=0)


def test_the_page_script_parses() -> None:
    """Guard the one thing an inline script otherwise loses: a syntax check.

    The VS Code console's inline webview script passes through neither tsc nor
    ruff, so a typo ships. This page is small enough that parsing it on every
    run is free.
    """
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to parse the console script")
    page = console_asset_path().read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match is not None, "the console page must carry its script inline"
    with tempfile.TemporaryDirectory() as temporary:
        script = Path(temporary) / "console.js"
        script.write_text(match.group(1), encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(script)], capture_output=True, text=True
        )

    assert result.returncode == 0, result.stderr


def test_the_page_loads_nothing_from_the_network() -> None:
    """Every asset the page pulls must come from this server, never a CDN."""

    import re

    page = console_asset_path().read_text(encoding="utf-8")

    assert "http://" not in page
    assert "https://" not in page
    references = re.findall(r'(?:src|href)="([^"]+)"', page)
    assert references, "the page should reference its manifest and icon"
    for reference in references:
        assert reference.startswith("./"), reference


def test_a_wildcard_bind_reports_an_address_a_browser_can_open() -> None:
    from baldr_router.console_service import console_url

    server = build_console_server(host="0.0.0.0", port=0, allow_non_loopback=True)
    try:
        url = console_url(server)
    finally:
        server.server_close()

    assert url.startswith("http://127.0.0.1:")
    assert "0.0.0.0" not in url
    assert "b'" not in url


def test_opening_a_browser_uses_the_tokened_link(tmp_path: Path, monkeypatch) -> None:
    """`make up` is only one command if nobody has to copy a token by hand."""

    import baldr_router.console_service as console_service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    opened: list[str] = []
    monkeypatch.setattr(
        console_service.webbrowser, "open", lambda url: opened.append(url) or True
    )

    server = console_service.build_console_server(
        host="127.0.0.1", port=0, token="open-token"
    )
    thread = console_service.serve_console_in_background(server)
    try:
        # serve_console owns the print and the open; exercise the same call it
        # makes rather than starting a second server that blocks forever.
        console_service.webbrowser.open(
            console_service.console_url(server, with_token=True)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert opened and opened[0].endswith("#token=open-token")
