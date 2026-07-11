from __future__ import annotations

from typing import Any


def provider_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    provider: str | None = None,
    runner: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "reason": message,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }
    if provider:
        payload["provider"] = provider
    if runner:
        payload["runner"] = runner
    if details:
        payload["error"]["details"] = details
    return payload
