from __future__ import annotations

from typing import Any

from .redaction import redact_text, redact_value


_ERROR_GUIDANCE: dict[str, tuple[str, str]] = {
    "codex_not_found": (
        "Codex CLI is not available in the selected runtime.",
        "Install Codex CLI in that runtime, then retry the saved work item.",
    ),
    "codex_not_authenticated": (
        "Codex cannot start because the selected runtime is not authenticated.",
        "Run `codex login` in that runtime, then retry the saved work item.",
    ),
    "codex_timeout": (
        "Codex exceeded the execution time limit and its process tree was stopped.",
        "Review the task scope or timeout, then use the durable retry action.",
    ),
    "codex_process_aborted": (
        "Codex stopped before it could return a complete result.",
        "Use the durable retry action; Baldr preserved the last confirmed state.",
    ),
    "codex_process_failed": (
        "Codex exited without completing the requested phase.",
        "Open the technical details, correct the reported cause, then retry.",
    ),
    "codex_invalid_structured_output": (
        "Codex completed, but its result did not satisfy the Baldr report contract.",
        "Retry the saved work item; if it repeats, inspect the validation details.",
    ),
    "provider_unexpected_exception": (
        "The selected provider failed unexpectedly.",
        "Open the technical details and retry the saved work item.",
    ),
    "automatic_settlement_failed": (
        "Baldr could not complete an automatic recovery action.",
        "The work item remains saved; open it, review the technical details, and retry.",
    ),
    "orphan_processes_detected": (
        "Baldr could not confirm that every process for the run stopped.",
        "Keep the work item stopped and inspect the process-cleanup evidence before retrying.",
    ),
}


def error_guidance(
    code: str,
    *,
    retryable: bool = False,
    summary: str | None = None,
    action: str | None = None,
) -> dict[str, str]:
    """Return stable user-facing copy without exposing technical exception prose."""

    default_summary = "The provider could not complete the operation."
    default_action = (
        "Use the durable retry action."
        if retryable
        else "Open the technical details and resolve the reported cause."
    )
    known_summary, known_action = _ERROR_GUIDANCE.get(
        str(code or "").strip(),
        (default_summary, default_action),
    )
    return {
        "summary": redact_text(str(summary or known_summary))[:1_000],
        "action": redact_text(str(action or known_action))[:1_000],
    }


def provider_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    provider: str | None = None,
    runner: str | None = None,
    details: dict[str, Any] | None = None,
    summary: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    technical_message = redact_text(str(message))
    guidance = error_guidance(
        code,
        retryable=retryable,
        summary=summary,
        action=action,
    )
    payload: dict[str, Any] = {
        "ok": False,
        "reason": technical_message,
        "error": {
            "code": code,
            "message": technical_message,
            "retryable": retryable,
            **guidance,
        },
    }
    if provider:
        payload["provider"] = provider
    if runner:
        payload["runner"] = runner
    if details:
        payload["error"]["details"] = redact_value(details)
    return payload
