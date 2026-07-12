from __future__ import annotations

from typing import Mapping


class InvalidStateTransition(RuntimeError):
    pass


RUN_TERMINAL = {
    "approved",
    "needs_changes",
    "blocked",
    "failed",
    "cancelled",
}

RUN_TRANSITIONS: Mapping[str, set[str]] = {
    "pending": {"running", "cancelled", "failed"},
    "running": {
        "finalizing",
        "approved",
        "needs_changes",
        "blocked",
        "failed",
        "cancelling",
        "interrupted",
        "unknown",
        "awaiting_reconciliation",
        "recovering",
    },
    "recovering": {
        "running",
        "interrupted",
        "unknown",
        "awaiting_reconciliation",
        "failed",
        "cancelling",
        "cancelled",
    },
    "finalizing": {
        "approved",
        "awaiting_reconciliation",
        "recovering",
        "interrupted",
        "unknown",
        "failed",
        "cancelling",
    },
    "cancelling": {"cancelled", "unknown", "failed"},
    "interrupted": {"recovering", "running", "cancelling", "cancelled", "failed", "unknown"},
    "unknown": {"recovering", "awaiting_reconciliation", "cancelling", "cancelled", "failed"},
    "awaiting_reconciliation": {
        "recovering",
        "running",
        "cancelling",
        "cancelled",
        "failed",
        "needs_changes",
        "approved",
    },
    "approved": set(),
    "needs_changes": set(),
    "blocked": set(),
    "failed": set(),
    "cancelled": set(),
}

STEP_TERMINAL = {"succeeded", "failed", "skipped", "cancelled"}
STEP_TRANSITIONS: Mapping[str, set[str]] = {
    "pending": {"dispatching", "running", "skipped", "cancelled", "failed"},
    "dispatching": {"running", "failed", "interrupted", "unknown", "cancelled"},
    "running": {"succeeded", "failed", "interrupted", "unknown", "cancelled"},
    "interrupted": {"pending", "running", "unknown", "cancelled", "failed"},
    "unknown": {"pending", "running", "cancelled", "failed"},
    "succeeded": set(),
    "failed": set(),
    "skipped": set(),
    "cancelled": set(),
}

ATTEMPT_TERMINAL = {"succeeded", "failed", "cancelled"}
ATTEMPT_TRANSITIONS: Mapping[str, set[str]] = {
    "dispatching": {"running", "succeeded", "failed", "interrupted", "unknown", "cancelled"},
    "running": {"succeeded", "failed", "interrupted", "unknown", "cancelled"},
    "interrupted": {"unknown", "cancelled", "failed"},
    "unknown": {"cancelled", "failed"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}

PARTICIPANT_TRANSITIONS = STEP_TRANSITIONS


def assert_transition(kind: str, current: str, target: str) -> None:
    maps = {
        "run": RUN_TRANSITIONS,
        "step": STEP_TRANSITIONS,
        "participant": PARTICIPANT_TRANSITIONS,
        "attempt": ATTEMPT_TRANSITIONS,
    }
    transition_map = maps[kind]
    if current == target:
        return
    if target not in transition_map.get(current, set()):
        raise InvalidStateTransition(
            f"Invalid {kind} transition: {current!r} -> {target!r}"
        )
