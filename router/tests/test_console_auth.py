"""The console's authentication and cross-site defences.

A localhost port is reachable from every page the operator visits, so these are
the tests that decide whether this surface can ever be allowed to write.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from baldr_router.console_service import (
    TOKEN_ENV,
    TOKEN_HEADER,
    build_console_server,
    console_url,
    new_console_token,
    serve_console_in_background,
)

TOKEN = "test-console-token"


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


def _request(
    url: str, *, headers: dict[str, str] | None = None, method: str = "GET"
) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    # Icons are binary, and no assertion here inspects their bytes.
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), dict(response.headers), body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return int(error.code), dict(error.headers), body


def _authorized(url: str, **extra: str) -> tuple[int, dict[str, str], str]:
    return _request(url, headers={TOKEN_HEADER: TOKEN, **extra})


def test_the_api_refuses_a_request_without_a_token(console: str) -> None:
    status, _, body = _request(f"{console}/v1/workbench")

    assert status == 401
    assert json.loads(body)["error"]["code"] == "console_token_required"


def test_the_api_refuses_a_wrong_token(console: str) -> None:
    status, _, _ = _request(
        f"{console}/v1/workbench", headers={TOKEN_HEADER: TOKEN + "x"}
    )

    assert status == 401


def test_the_api_accepts_the_session_token(console: str) -> None:
    status, _, body = _authorized(f"{console}/v1/workbench")

    assert status == 200
    assert json.loads(body)["intent"] == "status"


def test_a_token_in_the_query_string_is_not_accepted(console: str) -> None:
    """The token must never travel where a log or a Referer header can keep it."""

    status, _, _ = _request(f"{console}/v1/workbench?token={TOKEN}")

    assert status == 401


def test_a_cross_origin_request_is_refused_even_with_the_token(console: str) -> None:
    status, _, body = _authorized(f"{console}/v1/workbench", Origin="https://evil.test")

    assert status == 403
    assert json.loads(body)["error"]["code"] == "cross_origin"


def test_a_cross_site_fetch_is_refused_even_with_the_token(console: str) -> None:
    status, _, body = _authorized(
        f"{console}/v1/workbench", **{"Sec-Fetch-Site": "cross-site"}
    )

    assert status == 403
    assert json.loads(body)["error"]["code"] == "cross_site"


def test_a_rebound_dns_name_is_refused(console: str) -> None:
    """DNS rebinding points an attacker's name at loopback; Host gives it away."""

    status, _, body = _authorized(f"{console}/v1/workbench", Host="evil.test")

    assert status == 403
    assert json.loads(body)["error"]["code"] == "host_not_allowed"


def test_the_same_origin_of_this_server_is_accepted(console: str) -> None:
    status, _, _ = _authorized(
        f"{console}/v1/workbench",
        Origin=console,
        **{"Sec-Fetch-Site": "same-origin"},
    )

    assert status == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_are_authorized_before_anything_else(
    console: str, method: str
) -> None:
    """An anonymous caller learns nothing about which verbs exist."""

    unauthenticated, _, body = _request(f"{console}/v1/workbench", method=method)

    assert unauthenticated == 401
    assert json.loads(body)["error"]["code"] == "console_token_required"


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_verbs_without_a_route_are_refused(console: str, method: str) -> None:
    status, _, body = _request(
        f"{console}/v1/workbench", headers={TOKEN_HEADER: TOKEN}, method=method
    )

    assert status == 405
    assert json.loads(body)["error"]["code"] == "verb_not_supported"


def test_the_console_never_sets_a_cookie(console: str) -> None:
    """No ambient credential is the reason CSRF cannot reach this server."""

    for path in ("/", "/v1/workbench", "/manifest.webmanifest", "/livez"):
        _, headers, _ = _authorized(f"{console}{path}")
        assert "Set-Cookie" not in headers


def test_the_shell_stays_readable_without_a_token(console: str) -> None:
    """A bookmark must still load, so the page itself can explain the token."""

    for path, expected in (
        ("/", "text/html"),
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/sw.js", "text/javascript"),
        ("/icon-192.png", "image/png"),
    ):
        status, headers, _ = _request(f"{console}{path}")
        assert status == 200, path
        assert headers["Content-Type"].startswith(expected), path


def test_responses_carry_the_hardening_headers(console: str) -> None:
    _, headers, _ = _request(f"{console}/")
    policy = headers["Content-Security-Policy"]

    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'none'" in policy
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_the_startup_link_carries_the_token_in_the_fragment() -> None:
    server = build_console_server(host="127.0.0.1", port=0, token=TOKEN)
    try:
        url = console_url(server, with_token=True)
    finally:
        server.server_close()

    # A fragment is never sent to a server, so it cannot reach a log.
    assert f"#token={TOKEN}" in url
    assert "?" not in url


def test_a_pinned_token_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "pinned-token")
    assert new_console_token() == "pinned-token"

    monkeypatch.delenv(TOKEN_ENV)
    generated = new_console_token()
    assert len(generated) >= 32
    assert generated != new_console_token()
