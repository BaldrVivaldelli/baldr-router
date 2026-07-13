from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .redaction import REDACTED, redact_text

PROGRESS_CONTRACT = "baldr-work-item-progress"
PROGRESS_VERSION = 1

_STAGE_IDS = ("planning", "execution", "review")
_REPORT_STATUS_VALUES = {
    "planned",
    "implemented",
    "reviewed",
    "approved",
    "needs_changes",
    "partial",
    "blocked",
    "no_changes_needed",
    "failed",
    "cancelled",
    "inconclusive",
}
_REVIEW_DECISIONS = {
    "approved",
    "changes_required",
    "inconclusive",
    "not_applicable",
}
_ATTENTION_REPORT_STATUSES = {
    "needs_changes",
    "partial",
    "blocked",
    "failed",
    "inconclusive",
}
_RUNNING_STATUSES = {"dispatching", "running", "recovering", "finalizing", "cancelling"}
_ATTENTION_STATUSES = {
    "failed",
    "blocked",
    "needs_changes",
    "unknown",
    "interrupted",
    "awaiting_reconciliation",
}
_COMPLETE_STATUSES = {"succeeded", "approved", "completed"}

_ACTION_COPY: dict[str, tuple[str, str]] = {
    "inspect_shadow": (
        "Inspeccionar la copia protegida",
        "Revisá lo que hizo Baldr sin modificar tus archivos originales.",
    ),
    "continue_from_shadow": (
        "Continuar desde la copia",
        "Retomá el trabajo conservando los cambios protegidos.",
    ),
    "apply_shadow_changes": (
        "Aplicar los cambios",
        "Pasá a tu carpeta únicamente los cambios comprobados.",
    ),
    "discard_shadow": (
        "Descartar la copia",
        "Eliminá los cambios protegidos y mantené intacta tu carpeta.",
    ),
    "resume_from_checkpoint": (
        "Retomar desde el último punto seguro",
        "Volvé al último estado guardado por Baldr y continuá.",
    ),
    "accept_existing_changes": (
        "Conservar los cambios actuales",
        "Aceptá el estado que ya existe en la carpeta y continuá.",
    ),
    "discard_worktree": (
        "Descartar la copia de trabajo",
        "Eliminá la copia aislada sin cambiar la carpeta original.",
    ),
    "mark_failed": (
        "Dar la sesión por fallida",
        "Cerrá la sesión sin aplicar más cambios.",
    ),
    "start": ("Volver a intentar", "Iniciá nuevamente la sesión."),
    "cancel": ("Cancelar", "Pedile a Baldr que detenga el trabajo."),
    "archive": ("Archivar", "Guardá la sesión fuera de la lista principal."),
}

_ACTIVITY_COPY = {
    "working": {
        "planning": "Trabajando en la planificación",
        "execution": "Trabajando en la ejecución",
        "review": "Trabajando en la revisión",
        None: "Trabajando en la sesión",
    },
    "analyzing": {
        "planning": "Analizando el pedido",
        "execution": "Analizando cómo realizar los cambios",
        "review": "Analizando el resultado",
        None: "Analizando la sesión",
    },
    "researching": {
        "planning": "Buscando información útil para el plan",
        "execution": "Buscando información útil para los cambios",
        "review": "Buscando información útil para la revisión",
        None: "Buscando información útil",
    },
    "changing": {
        "planning": "Preparando el plan",
        "execution": "Realizando los cambios",
        "review": "Revisando los cambios realizados",
        None: "Realizando el trabajo",
    },
    "verifying": {
        "planning": "Comprobando el plan",
        "execution": "Comprobando los cambios",
        "review": "Comprobando el resultado",
        None: "Comprobando el resultado",
    },
}
_ACTIVITY_CATEGORIES = frozenset(_ACTIVITY_COPY)
_ACTIVITY_STATES = {"started", "running", "completed", "failed"}

_FILE_URI = re.compile(r"(?i)\bfile://[^\s<>{}\[\]\"']+")
_HOME_PATH = re.compile(r"(?<![A-Za-z0-9_.-])~[\\/][^\s<>{}\[\]\"']*")
_WINDOWS_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>{}\[\]\"']*"
)
_LABELLED_ABSOLUTE = re.compile(
    r"(?i)(\b[A-Za-z][A-Za-z0-9_.-]{0,63}\s*[=:]\s*)/"
    r"(?:[^\s<>{}\[\]\"']+)"
)
_POSIX_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_.:/-])/(?:[^\s<>{}\[\]\"']+)")
_PARENT_PATH = re.compile(r"(?<![A-Za-z0-9_.-])(?:\.\.[\\/])+(?:[^\s<>{}\[\]\"']*)")
_SAFE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _integer(value: Any, default: int = 0, *, maximum: int = 1_000_000) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(result, maximum))


def _token(value: Any, default: str = "") -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return normalized if _SAFE_TOKEN.fullmatch(normalized) else default


def _identifier(value: Any, *, limit: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > limit:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", candidate):
        return None
    return candidate


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    candidate = value.strip()
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return candidate


def _timestamp_value(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _first_timestamp(values: Sequence[Any]) -> str | None:
    parsed = [item for value in values if (item := _timestamp(value)) is not None]
    return min(parsed, key=_timestamp_value) if parsed else None


def _last_timestamp(values: Sequence[Any]) -> str | None:
    parsed = [item for value in values if (item := _timestamp(value)) is not None]
    return max(parsed, key=_timestamp_value) if parsed else None


def _strip_absolute_paths(value: str) -> str:
    result = _FILE_URI.sub("<ruta omitida>", value)
    result = _HOME_PATH.sub("<ruta omitida>", result)
    result = _WINDOWS_ABSOLUTE.sub("<ruta omitida>", result)
    result = _LABELLED_ABSOLUTE.sub(
        lambda match: f"{match.group(1)}<ruta omitida>", result
    )
    result = _PARENT_PATH.sub("<ruta omitida>", result)
    result = _POSIX_ABSOLUTE.sub("<ruta omitida>", result)
    return result


def _safe_text(value: Any, *, limit: int = 1_600) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = redact_text(str(value)).replace("\x00", "")
    text = "".join(
        character for character in text if character in "\n\t" or ord(character) >= 32
    )
    text = _strip_absolute_paths(text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _safe_relative_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().strip("`\"'")
    if not raw or len(raw) > 1_024 or "\x00" in raw:
        return None
    if (
        _FILE_URI.search(raw)
        or _WINDOWS_ABSOLUTE.search(raw)
        or raw.startswith(("/", "~"))
    ):
        return None
    normalized = raw.replace("\\", "/")
    if ":" in normalized:
        # Reject URI schemes and labelled absolute paths instead of returning a
        # cleaned value that could still be mistaken for a repository path.
        return None
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    safe = _safe_text(normalized, limit=320)
    if not safe or "<ruta omitida>" in safe or REDACTED in safe:
        return None
    return safe


def _safe_string_list(
    value: Any,
    *,
    limit: int = 24,
    text_limit: int = 500,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for entry in _items(value)[:limit]:
        text = _safe_text(entry, limit=text_limit)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_files(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for entry in _items(value)[:100]:
        path = _safe_relative_path(entry)
        if path and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _safe_decisions(value: Any) -> list[dict[str, str]]:
    pairs: list[tuple[Any, Any]] = []
    if isinstance(value, Mapping):
        pairs = list(value.items())
    else:
        for entry in _items(value):
            item = _record(entry)
            if item:
                pairs.append(
                    (
                        item.get("key") or item.get("title"),
                        item.get("value") or item.get("detail"),
                    )
                )
    result: list[dict[str, str]] = []
    for key, decision in pairs[:24]:
        safe_key = _safe_text(key, limit=160)
        safe_value = _safe_text(decision, limit=700)
        if safe_key and safe_value:
            result.append({"key": safe_key, "value": safe_value})
    return result


def _deliverable_descriptors(value: Any) -> list[dict[str, Any]]:
    """Project bounded selectors only; source run/step/artifact IDs stay private."""

    result: list[dict[str, Any]] = []
    for raw in _items(value)[:256]:
        descriptor = _record(raw)
        stage = _token(descriptor.get("stage"))
        availability = _token(descriptor.get("availability"))
        digest = str(descriptor.get("digest") or "").strip().lower()
        if stage not in _STAGE_IDS or availability not in {
            "available",
            "summary_only",
            "unavailable",
        }:
            continue
        preview_raw = _record(descriptor.get("preview"))
        preview = None
        if preview_raw:
            preview = {
                "status": _token(preview_raw.get("status")) or None,
                "summary": _safe_text(preview_raw.get("summary"), limit=2_400),
                "review_decision": _token(preview_raw.get("review_decision")) or None,
            }
        result.append(
            {
                "stage": stage,
                "round": _integer(descriptor.get("round"), maximum=10_000),
                "run_ordinal": max(
                    1, _integer(descriptor.get("run_ordinal"), 1, maximum=10_000)
                ),
                "item_revision": max(
                    1, _integer(descriptor.get("item_revision"), 1, maximum=10_000)
                ),
                "availability": availability,
                "reason": _token(descriptor.get("reason")) or None,
                "digest": digest if re.fullmatch(r"[a-f0-9]{64}", digest) else None,
                "redacted": True,
                "created_at": _timestamp(descriptor.get("created_at")),
                "preview": preview,
                "entry_count": _integer(
                    descriptor.get("entry_count"), maximum=100_000
                ),
                "action": "inspect-item-phase",
            }
        )
    return result


def _deliverable_index(value: Any, *, returned: int) -> dict[str, Any]:
    index = _record(value)
    safe_returned = max(0, min(returned, 256))
    total = max(safe_returned, _integer(index.get("total"), safe_returned))
    cursor_value = index.get("next_cursor")
    cursor = (
        cursor_value.strip()
        if isinstance(cursor_value, str)
        and 0 < len(cursor_value.strip()) <= 1_024
        and re.fullmatch(r"[A-Za-z0-9_-]+", cursor_value.strip())
        else None
    )
    truncated = total > safe_returned
    return {
        "total": total,
        "returned": safe_returned,
        "truncated": truncated,
        "next_cursor": cursor if truncated else None,
        "action": "list-item-deliverables",
    }


def _report_candidate(value: Any) -> dict[str, Any]:
    record = _record(value)
    if not record:
        return {}
    nested = _record(record.get("final_report"))
    if nested:
        return nested
    nested = _record(record.get("report"))
    if nested:
        return nested
    if any(
        key in record
        for key in (
            "summary",
            "decisions",
            "blockers",
            "review_decision",
            "acceptance_criteria",
        )
    ):
        return record
    return {}


def _step_report(step: Mapping[str, Any]) -> dict[str, Any]:
    direct = _report_candidate(step.get("output"))
    if direct:
        return direct
    for participant_value in reversed(_items(step.get("participants"))):
        participant = _record(participant_value)
        candidate = _report_candidate(participant.get("result"))
        if candidate:
            return candidate
    output = _record(step.get("output"))
    if output.get("reason") or step.get("error_reason"):
        # Exception/provider prose is technical and can contain private paths or
        # implementation details. The public surface uses canonical copy and the
        # allowlisted error code retained under ``technical`` instead.
        return {
            "status": "failed",
            "summary": "La etapa no pudo completarse.",
            "blockers": [],
        }
    return {}


def normalize_public_report(value: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return a bounded public report and its explicitly technical supplement."""

    report = _report_candidate(value)
    if not report:
        return None, {"commands_run": []}
    status = _token(report.get("status"))
    public: dict[str, Any] = {
        "evidence": "reported",
        "status": status if status in _REPORT_STATUS_VALUES else None,
        "summary": _safe_text(report.get("summary"), limit=2_400),
        "interpretation": _safe_text(report.get("interpretation"), limit=2_400),
        "scope": _safe_string_list(report.get("scope")),
        "approach": _safe_string_list(report.get("approach")),
        "plan_steps": _safe_string_list(report.get("plan_steps")),
        "work_completed": _safe_string_list(report.get("work_completed")),
        "work_next": _safe_string_list(report.get("work_next")),
        "findings": _safe_string_list(report.get("findings")),
        "corrections": _safe_string_list(report.get("corrections")),
        "verification_evidence": _safe_string_list(
            report.get("verification_evidence")
        ),
        "decisions": _safe_decisions(report.get("decisions")),
        "acceptance_criteria": _safe_string_list(report.get("acceptance_criteria")),
        "assumptions": _safe_string_list(report.get("assumptions")),
        "files_modified": _safe_files(report.get("files_modified")),
        "tests_run": _safe_string_list(report.get("tests_run")),
        "verification_needed": _safe_string_list(report.get("verification_needed")),
        "risks": _safe_string_list(report.get("risks")),
        "follow_up": _safe_string_list(report.get("follow_up")),
        "blockers": _safe_string_list(report.get("blockers")),
        "review_decision": None,
    }
    decision = _token(report.get("review_decision"))
    if decision in _REVIEW_DECISIONS:
        public["review_decision"] = decision
    technical = {
        "commands_run": _safe_string_list(
            report.get("commands_run"), limit=30, text_limit=700
        ),
        "constraints": _safe_string_list(report.get("constraints")),
        "alternatives_rejected": _safe_string_list(report.get("alternatives_rejected")),
    }
    return public, technical


def _stage_for_step(step: Mapping[str, Any]) -> str | None:
    phase = _token(step.get("phase"))
    key = _safe_text(step.get("step_key"), limit=120).lower()
    if phase in {"architect", "architecture", "planning", "plan"} or "architect" in key:
        return "planning"
    if phase in {"implementer", "implementation", "execution", "fix"} or any(
        marker in key for marker in ("implementer", "implementation", ".fix", "fix_")
    ):
        return "execution"
    if phase in {"reviewer", "review"} or "review" in key:
        return "review"
    return None


def _step_sort_key(step: Mapping[str, Any]) -> tuple[int, int, float]:
    created = _timestamp(step.get("created_at"))
    return (
        _integer(step.get("sequence_number")),
        _integer(step.get("round_number")),
        _timestamp_value(created),
    )


def _semantic_step_state(
    step: Mapping[str, Any], report: Mapping[str, Any] | None
) -> str:
    status = _token(step.get("status"), "pending")
    if status in _RUNNING_STATUSES:
        return "running"
    if status in {"cancelled"}:
        return "cancelled"
    if status in {"skipped"}:
        return "skipped"
    if status in _ATTENTION_STATUSES:
        return "attention"
    if report:
        report_status = _token(report.get("status"))
        review_decision = _token(report.get("review_decision"))
        if (
            report_status in _ATTENTION_REPORT_STATUSES
            or review_decision in {"changes_required", "inconclusive"}
            or bool(_items(report.get("blockers")))
        ):
            return "attention"
    if status in _COMPLETE_STATUSES:
        return "complete"
    return "pending"


def _explicit_retryable(value: Any, *, depth: int = 0) -> bool | None:
    """Read only an explicit provider/attempt retryability boolean.

    Retryability is a safety capability, not something the public projector can
    infer from an error code or prose.  Walk only the bounded result containers
    used by durable attempts and reduced phase outputs, preferring the most
    specific ``error.retryable`` value.  Arbitrary report fields are ignored.
    """

    if depth > 5:
        return None
    record = _record(value)
    if not record:
        return None
    error = _record(record.get("error"))
    if isinstance(error.get("retryable"), bool):
        return bool(error["retryable"])
    if isinstance(record.get("retryable"), bool):
        return bool(record["retryable"])
    for key in ("result", "output"):
        nested = _explicit_retryable(record.get(key), depth=depth + 1)
        if nested is not None:
            return nested
    for key in ("attempts", "participants", "failures"):
        for entry in reversed(_items(record.get(key))[-24:]):
            nested = _explicit_retryable(entry, depth=depth + 1)
            if nested is not None:
                return nested
    return None


def _retryability(
    run: Mapping[str, Any], ordered_steps: Sequence[Mapping[str, Any]]
) -> bool | None:
    """Return retryability only when the current failure recorded evidence."""

    direct = _explicit_retryable(run)
    if direct is not None:
        return direct
    current_step_id = run.get("current_step_id")
    if isinstance(current_step_id, str):
        current = next(
            (step for step in reversed(ordered_steps) if step.get("id") == current_step_id),
            None,
        )
        if current is not None:
            explicit = _explicit_retryable(current)
            if explicit is not None:
                return explicit
    for step in reversed(ordered_steps[-12:]):
        if _token(step.get("status")) not in _ATTENTION_STATUSES | {"failed"}:
            continue
        explicit = _explicit_retryable(step)
        if explicit is not None:
            return explicit
    return None


def _history_entry(step: Mapping[str, Any]) -> dict[str, Any]:
    raw_report = _step_report(step)
    report, report_technical = normalize_public_report(raw_report)
    state = _semantic_step_state(step, raw_report)
    participants = [_record(value) for value in _items(step.get("participants"))]
    attempts = sum(
        max(
            _integer(participant.get("attempt_count")),
            len(_items(participant.get("attempts"))),
        )
        for participant in participants
    )
    errors = sorted(
        {
            code
            for raw in [
                step.get("error_code"),
                *(participant.get("error_code") for participant in participants),
            ]
            if (code := _token(raw))
        }
    )[:12]
    public_participants: list[dict[str, Any]] = []
    for participant in participants[:16]:
        profile = _safe_text(participant.get("profile_name"), limit=96)
        provider = _safe_text(participant.get("provider"), limit=64)
        model_or_agent = _safe_text(
            participant.get("model") or participant.get("agent"), limit=128
        )
        public_participants.append(
            {
                "profile": profile or None,
                "provider": provider or None,
                "model_or_agent": model_or_agent or None,
                "state": _token(participant.get("status"), "unknown"),
                "attempt_count": max(
                    _integer(participant.get("attempt_count")),
                    len(_items(participant.get("attempts"))),
                ),
            }
        )
    technical = {
        "step_count": 1,
        "participant_count": len(participants),
        "attempt_count": attempts,
        "error_codes": errors,
        "participants": public_participants,
        **report_technical,
    }
    return {
        "round": _integer(step.get("round_number")),
        "state": state,
        "outcome": (report or {}).get("status"),
        "started_at": _timestamp(step.get("started_at")),
        "completed_at": _timestamp(step.get("completed_at")),
        "report": report,
        "technical": technical,
    }


def _stage_state(history: list[dict[str, Any]]) -> str:
    if not history:
        return "pending"
    # Steps for one stage are ordered attempts/rounds.  A stale earlier
    # ``running`` row must not override the state of the current retry.
    return str(history[-1]["state"])


def _stage_projection(stage_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(steps, key=_step_sort_key)
    full_history = [_history_entry(step) for step in ordered]
    # ``report`` is the selected/latest round. ``history`` intentionally holds
    # only earlier rounds so clients do not render the current result twice.
    history = full_history[:-1][-12:]
    state = _stage_state(full_history)
    # Public current-state fields describe only the selected/latest round.  In
    # particular, a new retry that has not reported yet must not appear to have
    # inherited the result, commands, outcome, or completion time of a prior
    # attempt.  Earlier results remain available under ``history``.
    current = full_history[-1] if full_history else {}
    latest_report = current.get("report")
    latest_technical = _record(current.get("technical"))
    started_at = _first_timestamp([step.get("started_at") for step in ordered])
    completed_at = current.get("completed_at") if state in {
        "complete",
        "attention",
        "cancelled",
        "skipped",
    } else None
    participants_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in full_history:
        for raw_participant in _items(entry["technical"].get("participants")):
            participant = _record(raw_participant)
            identity = (
                participant.get("profile"),
                participant.get("provider"),
                participant.get("model_or_agent"),
            )
            current = participants_by_identity.get(identity)
            if current is None:
                participants_by_identity[identity] = participant
            else:
                current["state"] = participant.get("state")
                current["attempt_count"] = _integer(
                    _integer(current.get("attempt_count"))
                    + _integer(participant.get("attempt_count"))
                )
    return {
        "id": stage_id,
        "state": state,
        "outcome": (latest_report or {}).get("status")
        if state in {"complete", "attention", "cancelled", "skipped"}
        else None,
        "round_count": len(ordered),
        "started_at": started_at,
        "completed_at": completed_at,
        "report": latest_report,
        "evidence": "reported" if latest_report else "observed",
        "history": history,
        "technical": {
            "step_count": len(ordered),
            "participant_count": sum(
                int(entry["technical"].get("participant_count") or 0)
                for entry in full_history
            ),
            "attempt_count": sum(
                int(entry["technical"].get("attempt_count") or 0)
                for entry in full_history
            ),
            "error_codes": sorted(
                {
                    code
                    for entry in full_history
                    for code in _items(entry["technical"].get("error_codes"))
                    if isinstance(code, str)
                }
            )[:12],
            "commands_run": list(latest_technical.get("commands_run") or []),
            "constraints": list(latest_technical.get("constraints") or []),
            "alternatives_rejected": list(
                latest_technical.get("alternatives_rejected") or []
            ),
            "participants": list(participants_by_identity.values())[:24],
        },
    }


def _overall_state(item: Mapping[str, Any], run: Mapping[str, Any]) -> str:
    run_status = _token(run.get("status"))
    item_status = _token(item.get("status"), "draft")
    if item_status == "archived":
        return "archived"
    if run_status:
        if run_status == "approved":
            return "complete"
        if run_status == "cancelled":
            return "cancelled"
        if run_status in _ATTENTION_STATUSES:
            return "attention"
        if run_status in _RUNNING_STATUSES:
            return "running"
        return "pending"
    if item_status == "completed":
        return "complete"
    if item_status == "cancelled":
        return "cancelled"
    if item_status in {"needs_attention", "failed"}:
        return "attention"
    if item_status in {"running", "cancelling"}:
        return "running"
    return "pending"


def _event_stage(
    event: Mapping[str, Any], step_stages: Mapping[str, str]
) -> str | None:
    step_id = event.get("step_id")
    if isinstance(step_id, str) and step_id in step_stages:
        return step_stages[step_id]
    return None


def _activity_event(
    event: Mapping[str, Any], step_stages: Mapping[str, str]
) -> dict[str, Any] | None:
    if str(event.get("event_type") or "") != "phase.activity":
        return None
    payload = _record(event.get("payload"))
    category = _token(payload.get("category") or payload.get("kind"))
    if category not in _ACTIVITY_CATEGORIES:
        return None
    state = _token(payload.get("state"), "running")
    if state not in _ACTIVITY_STATES:
        state = "running"
    occurred_at = _timestamp(event.get("created_at"))
    if occurred_at is None:
        return None
    return {
        "kind": category,
        "message": _ACTIVITY_COPY[category][_event_stage(event, step_stages)],
        "since": occurred_at,
        "state": state,
        "stage": _event_stage(event, step_stages),
        "evidence": "observed",
    }


def _fallback_activity(
    overall: str,
    active_stage: str | None,
    run_status: str,
    stages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if run_status == "finalizing":
        return {
            "kind": "publishing",
            "message": "Preparando la entrega",
            "since": None,
            "evidence": "observed",
        }
    if run_status == "recovering":
        return {
            "kind": "recovering",
            "message": "Retomando el trabajo",
            "since": None,
            "evidence": "observed",
        }
    if run_status == "cancelling":
        return {
            "kind": "cancelling",
            "message": "Deteniendo el trabajo",
            "since": None,
            "evidence": "observed",
        }
    if overall == "complete":
        return {
            "kind": "completed",
            "message": "Trabajo completado",
            "since": None,
            "evidence": "observed",
        }
    if overall == "cancelled":
        return {
            "kind": "cancelled",
            "message": "Trabajo cancelado",
            "since": None,
            "evidence": "observed",
        }
    if overall == "archived":
        return {
            "kind": "archived",
            "message": "Sesión archivada",
            "since": None,
            "evidence": "observed",
        }
    if overall == "attention":
        return {
            "kind": "attention",
            "message": "Baldr necesita tu atención",
            "since": None,
            "evidence": "observed",
        }
    if overall == "pending" and run_status == "pending":
        return {
            "kind": "waiting",
            "message": "Sesión en espera para comenzar",
            "since": None,
            "evidence": "observed",
        }
    if overall == "running" and active_stage is None:
        return {
            "kind": "preparing_workspace",
            "message": "Preparando el trabajo",
            "since": None,
            "evidence": "observed",
        }
    if active_stage in _STAGE_IDS:
        return {
            "kind": "working",
            "message": _ACTIVITY_COPY["working"][active_stage],
            "since": stages[active_stage].get("started_at"),
            "evidence": "observed",
        }
    return {
        "kind": "waiting",
        "message": "Lista para empezar",
        "since": None,
        "evidence": "observed",
    }


def _active_stage(
    overall: str,
    run: Mapping[str, Any],
    ordered_steps: list[dict[str, Any]],
    stages: Mapping[str, Mapping[str, Any]],
) -> str | None:
    current_step_id = run.get("current_step_id")
    if isinstance(current_step_id, str):
        for step in ordered_steps:
            if step.get("id") == current_step_id:
                stage = _stage_for_step(step)
                if stage and stages[stage]["state"] in {"running", "attention"}:
                    return stage
    for stage_id in _STAGE_IDS:
        if stages[stage_id]["state"] == "running":
            return stage_id
    if overall == "attention":
        for stage_id in reversed(_STAGE_IDS):
            if stages[stage_id]["state"] == "attention":
                return stage_id
        # Publication conflicts, interrupted orchestration and other workflow
        # concerns are global.  Never assign them to a phase that completed
        # successfully (or to planning merely because no phase exists).
        return None
    if overall == "pending":
        # A durable run can exist in the queue before any phase has actually
        # started.  Do not present planning as active until a step says so.
        return None
    return None


def _milestones(
    snapshot: Mapping[str, Any],
    step_stages: Mapping[str, str],
    steps_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    lifecycle = {
        "workflow.created": ("created", None, "pending", "Sesión preparada"),
        "workflow.running": ("started", None, "running", "Baldr comenzó a trabajar"),
        "workflow.recovery_started": (
            "recovery",
            None,
            "running",
            "Baldr retomó el trabajo",
        ),
        "workflow.finalization_started": (
            "publishing",
            None,
            "running",
            "Preparando la entrega",
        ),
        "workflow.publication_requires_reconciliation": (
            "attention",
            None,
            "attention",
            "La entrega necesita tu atención",
        ),
        "workflow.shadow_retained_for_reconciliation": (
            "attention",
            None,
            "attention",
            "Los cambios quedaron protegidos para que los revises",
        ),
        "workflow.approved": ("completed", None, "complete", "Trabajo completado"),
        "workflow.needs_changes": (
            "attention",
            "review",
            "attention",
            "La revisión encontró cambios pendientes",
        ),
        "workflow.blocked": (
            "attention",
            None,
            "attention",
            "El trabajo quedó bloqueado",
        ),
        "workflow.failed": (
            "attention",
            None,
            "attention",
            "Baldr no pudo completar la sesión",
        ),
        "workflow.cancelled": ("cancelled", None, "cancelled", "Trabajo cancelado"),
    }
    step_events = {
        "step.running": ("stage_started", "running", "Etapa iniciada"),
        "step.succeeded": ("stage_completed", "complete", "Etapa completada"),
        "step.failed": ("stage_attention", "attention", "La etapa necesita atención"),
        "step.cancelled": ("stage_cancelled", "cancelled", "Etapa cancelada"),
        "step.skipped": ("stage_skipped", "skipped", "Etapa omitida"),
    }
    for raw_event in _items(snapshot.get("events")):
        event = _record(raw_event)
        occurred_at = _timestamp(event.get("created_at"))
        if occurred_at is None:
            continue
        # Provider activity belongs to the live ``activity`` slot.  It is not
        # a lifecycle milestone and must never evict durable starts, finishes,
        # recovery or publication evidence from this bounded list.
        if _activity_event(event, step_stages):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type in lifecycle:
            kind, stage, state, message = lifecycle[event_type]
        elif event_type in step_events:
            kind, state, message = step_events[event_type]
            stage = _event_stage(event, step_stages)
            if stage is None:
                continue
            step_id = event.get("step_id")
            step = steps_by_id.get(step_id, {}) if isinstance(step_id, str) else {}
            if event_type == "step.succeeded":
                raw_report = _step_report(step)
                semantic_state = _semantic_step_state(step, raw_report)
                if semantic_state == "attention":
                    kind = "review_changes" if stage == "review" else "stage_attention"
                    state = "attention"
                    message = (
                        "La revisión encontró cambios pendientes"
                        if stage == "review"
                        else "La etapa terminó con puntos pendientes"
                    )
                else:
                    message = {
                        "planning": "Planificación completada",
                        "execution": "Ejecución completada",
                        "review": "Revisión completada",
                    }[stage]
        else:
            continue
        result.append(
            {
                "kind": kind,
                "stage": stage,
                "state": state,
                "message": message,
                "at": occurred_at,
                "evidence": "observed",
            }
        )
    for raw_checkpoint in _items(snapshot.get("checkpoints")):
        checkpoint = _record(raw_checkpoint)
        status = _token(checkpoint.get("status"))
        occurred_at = _timestamp(checkpoint.get("verified_at"))
        if (
            status not in {"checkpointed", "verified", "published"}
            or occurred_at is None
        ):
            continue
        step_id = checkpoint.get("step_id")
        stage = step_stages.get(step_id) if isinstance(step_id, str) else None
        result.append(
            {
                "kind": "checkpoint_verified",
                "stage": stage,
                "state": "complete",
                "message": "Baldr guardó un punto seguro verificado",
                "at": occurred_at,
                "evidence": "verified",
            }
        )
    for raw_publication in _items(snapshot.get("publications")):
        publication = _record(raw_publication)
        if _token(publication.get("status")) != "published":
            continue
        occurred_at = _timestamp(
            publication.get("completed_at") or publication.get("updated_at")
        )
        if occurred_at is None:
            continue
        result.append(
            {
                "kind": "publication_verified",
                "stage": None,
                "state": "complete",
                "message": "Los cambios verificados se aplicaron correctamente",
                "at": occurred_at,
                "evidence": "verified",
            }
        )
    result.sort(key=lambda entry: _timestamp_value(entry.get("at")))
    return result[-40:]


def _attention(
    item: Mapping[str, Any],
    run: Mapping[str, Any],
    stages: Mapping[str, Mapping[str, Any]],
    active_stage: str | None,
    retryable: bool | None,
) -> dict[str, Any] | None:
    run_status = _token(run.get("status"))
    item_status = _token(item.get("status"))
    if run_status not in _ATTENTION_STATUSES and item_status not in {
        "needs_attention",
        "failed",
    }:
        return None
    if run_status == "awaiting_reconciliation":
        kind = "reconciliation"
        fallback = (
            "Los cambios están protegidos y Baldr necesita que elijas cómo continuar."
        )
    elif run_status == "needs_changes":
        kind = "changes_requested"
        fallback = "La revisión encontró puntos que todavía necesitan cambios."
    elif run_status == "blocked":
        kind = "blocked"
        fallback = "Baldr encontró un bloqueo y no puede continuar sin ayuda."
    elif run_status in {"unknown", "interrupted"}:
        kind = "interrupted"
        fallback = (
            "El trabajo se interrumpió y Baldr necesita confirmar cómo retomarlo."
        )
    else:
        kind = "failed"
        fallback = "Baldr no pudo completar la sesión."
    error_code = _token(run.get("error_code") or item.get("error_code"))
    if kind == "reconciliation" and error_code == "workflow_review_needs_changes":
        summary = (
            "La revisión encontró puntos que todavía necesitan cambios. "
            "El trabajo quedó protegido para que decidas cómo continuar."
        )
    elif kind == "reconciliation" and (
        ("publication" in error_code and "conflict" in error_code)
        or "source_changed" in error_code
        or "workspace_changed" in error_code
    ):
        summary = (
            "Tus archivos cambiaron mientras Baldr trabajaba; no los sobrescribimos."
        )
    elif kind == "blocked" and (
        error_code.startswith("provider_")
        or "isolation" in error_code
        or "config" in error_code
    ):
        summary = (
            "La configuración elegida no permite trabajar de forma segura. "
            "Revisá las opciones del equipo."
        )
    else:
        summary = fallback
    blockers: list[str] = []
    for stage_id in reversed(_STAGE_IDS):
        report = _record(stages[stage_id].get("report"))
        blockers.extend(_safe_string_list(report.get("blockers"), limit=12))
        if blockers:
            break
    actions: list[dict[str, str]] = []
    for raw_action in _items(item.get("allowed_actions")):
        action = _token(raw_action)
        if action not in _ACTION_COPY:
            continue
        if action == "start" and retryable is not True:
            # A generic "start" capability is not proof that repeating the
            # failed operation is safe or useful.  Offer retry only when the
            # recorded provider/attempt error explicitly says it is retryable.
            continue
        label, description = _ACTION_COPY[action]
        actions.append({"id": action, "label": label, "description": description})
    return {
        "required": True,
        "kind": kind,
        "stage": active_stage,
        "summary": summary,
        "blockers": blockers[:12],
        "actions": actions,
        "retryable": retryable,
    }


def _dedupe_strings(values: Sequence[Any], *, limit: int = 40) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _final_report(
    snapshot: Mapping[str, Any], stages: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    run = _record(snapshot.get("run"))
    base, _technical = normalize_public_report(run.get("final"))
    reports = [
        _record(stages[stage_id].get("report"))
        for stage_id in _STAGE_IDS
        if _record(stages[stage_id].get("report"))
    ]
    if base is None and not reports:
        return None
    base = base or {
        "evidence": "reported",
        "status": None,
        "summary": "",
        "interpretation": "",
        "scope": [],
        "approach": [],
        "plan_steps": [],
        "work_completed": [],
        "work_next": [],
        "findings": [],
        "corrections": [],
        "verification_evidence": [],
        "decisions": [],
        "acceptance_criteria": [],
        "assumptions": [],
        "files_modified": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "blockers": [],
        "review_decision": None,
    }
    execution = _record(stages.get("execution", {}).get("report"))
    review = _record(stages.get("review", {}).get("report"))
    planning = _record(stages.get("planning", {}).get("report"))
    # The workflow-level summary is intentionally generic. The implementation
    # report is the most useful plain-language description of what was delivered;
    # review is the fallback for read-only/reporting workflows.
    summary = str(
        execution.get("summary") or review.get("summary") or base.get("summary") or ""
    )
    decisions: list[dict[str, str]] = []
    seen_decisions: set[tuple[str, str]] = set()
    for report in [base, *reports]:
        for raw_decision in _items(report.get("decisions")):
            decision = _record(raw_decision)
            key = _safe_text(decision.get("key"), limit=160)
            value = _safe_text(decision.get("value"), limit=700)
            marker = (key, value)
            if not key or not value or marker in seen_decisions:
                continue
            seen_decisions.add(marker)
            decisions.append({"key": key, "value": value})
            if len(decisions) >= 24:
                break
    merged: dict[str, Any] = {
        "evidence": "reported",
        "status": base.get("status")
        or (review.get("status") if review else None)
        or (execution.get("status") if execution else None)
        or next(
            (
                report.get("status")
                for report in reversed(reports)
                if report.get("status")
            ),
            None,
        )
        or (
            run_status
            if (run_status := _token(run.get("status"))) in _REPORT_STATUS_VALUES
            else None
        )
        or None,
        "summary": summary,
        "interpretation": str(
            planning.get("interpretation") or base.get("interpretation") or ""
        ),
        "decisions": decisions,
        "review_decision": review.get("review_decision") or base.get("review_decision"),
    }
    sources_by_field: dict[str, list[dict[str, Any]]] = {
        "scope": [planning, base],
        "approach": [planning, execution, base],
        "plan_steps": [planning, base],
        "work_completed": [execution, review, base],
        "work_next": [execution, review, base],
        "findings": [review, base],
        "corrections": [execution, review, base],
        "verification_evidence": [execution, review, base],
        "acceptance_criteria": [base, *reports],
        "assumptions": [base, *reports],
        # Planning may mention likely files. Only implementation is evidence of
        # files actually reported as changed.
        "files_modified": [base, execution] if execution else [base],
        "tests_run": [base, execution, review],
        "verification_needed": [base, *reports],
        "risks": [base, *reports],
        "follow_up": [base, *reports],
        "blockers": [base, *reports],
    }
    for field, sources in sources_by_field.items():
        merged[field] = _dedupe_strings(
            [
                entry
                for report in sources
                if report
                for entry in _items(report.get(field))
            ],
            limit=100 if field == "files_modified" else 40,
        )
    return merged


def _revision(
    item: Mapping[str, Any], snapshot: Mapping[str, Any], last_event_at: str | None
) -> int:
    run = _record(snapshot.get("run"))
    timestamps = [
        last_event_at,
        run.get("updated_at"),
        item.get("updated_at"),
        run.get("completed_at"),
    ]
    latest = _last_timestamp(timestamps)
    epoch_ms = int(_timestamp_value(latest) * 1_000) if latest else 0
    sequences = [
        _integer(_record(event).get("sequence"), maximum=2_147_483_647)
        for event in _items(snapshot.get("events"))
    ]
    sequence = max(sequences, default=0) % 1_000
    return min(
        9_007_199_254_740_991,
        max(_integer(item.get("revision")), epoch_ms * 1_000 + sequence),
    )


def project_work_item_progress(
    item_value: Mapping[str, Any] | None,
    snapshot_value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the bounded, public and restart-stable work-item progress view.

    The projector deliberately ignores prompts, raw provider events, provider
    prose outside the structured report, paths from operational metadata, and
    reconciliation/checkpoint bodies. It performs no I/O and mutates no input.
    """

    item = _record(item_value)
    snapshot = _record(snapshot_value)
    run = _record(snapshot.get("run")) or _record(item.get("run"))
    steps = [_record(value) for value in _items(snapshot.get("steps"))]
    ordered_steps = sorted((step for step in steps if step), key=_step_sort_key)
    grouped: dict[str, list[dict[str, Any]]] = {stage_id: [] for stage_id in _STAGE_IDS}
    step_stages: dict[str, str] = {}
    for step in ordered_steps:
        stage_id = _stage_for_step(step)
        if stage_id is None:
            continue
        grouped[stage_id].append(step)
        step_id = step.get("id")
        if isinstance(step_id, str):
            step_stages[step_id] = stage_id
    stage_map = {
        stage_id: _stage_projection(stage_id, grouped[stage_id])
        for stage_id in _STAGE_IDS
    }
    overall = _overall_state(item, run)
    run_status = _token(run.get("status"))

    current_step = next(
        (
            step
            for step in reversed(ordered_steps)
            if step.get("id") == run.get("current_step_id")
        ),
        None,
    )
    current_stage = _stage_for_step(current_step) if current_step else None
    if run_status == "recovering" and current_stage is not None:
        # An unknown/interrupted attempt is being recovered, not reviewed for
        # corrections. Keep the durable phase association while presenting the
        # current round as active until recovery produces a new terminal fact.
        if _token(current_step.get("status")) in {
            "pending",
            "unknown",
            "interrupted",
            "recovering",
        }:
            stage_map[current_stage]["state"] = "running"
            stage_map[current_stage]["outcome"] = None
            stage_map[current_stage]["completed_at"] = None

    if overall == "cancelled":
        for stage in stage_map.values():
            if stage["state"] in {"pending", "running"}:
                stage["state"] = "cancelled"
                stage["outcome"] = "cancelled"
                stage["completed_at"] = _timestamp(run.get("completed_at"))
    elif (
        overall == "attention"
        and run_status not in {"awaiting_reconciliation", "unknown", "interrupted"}
        and not any(
            stage["state"] == "attention" for stage in stage_map.values()
        )
    ):
        # Only attribute attention to a recorded current phase. Workflow-level
        # failures before/after phases remain global instead of blaming planning.
        if current_stage is not None:
            stage_map[current_stage]["state"] = "attention"
            stage_map[current_stage]["outcome"] = run_status or "failed"
            stage_map[current_stage]["completed_at"] = _timestamp(
                run.get("updated_at")
            )

    active_stage = _active_stage(overall, run, ordered_steps, stage_map)
    steps_by_id = {
        str(step["id"]): step
        for step in ordered_steps
        if isinstance(step.get("id"), str)
    }
    milestones = _milestones(snapshot, step_stages, steps_by_id)
    activity_events = [
        value
        for raw in _items(snapshot.get("events"))
        if (value := _activity_event(_record(raw), step_stages)) is not None
        and overall == "running"
        and run_status == "running"
        and active_stage is not None
        and value.get("stage") in {None, active_stage}
        and _timestamp_value(value.get("since"))
        >= _timestamp_value(stage_map[active_stage].get("started_at"))
    ]
    activity = (
        max(activity_events, key=lambda entry: _timestamp_value(entry.get("since")))
        if activity_events
        else _fallback_activity(overall, active_stage, run_status, stage_map)
    )
    if activity.get("stage") is None and active_stage is not None:
        activity["stage"] = active_stage
    last_event_at = _last_timestamp(
        [_record(event).get("created_at") for event in _items(snapshot.get("events"))]
    ) or _last_timestamp([run.get("updated_at"), item.get("updated_at")])
    retryable = _retryability(run, ordered_steps)
    attention = _attention(item, run, stage_map, active_stage, retryable)
    final_report = _final_report(snapshot, stage_map)
    error_codes = sorted(
        {
            code
            for raw in (run.get("error_code"), item.get("error_code"))
            if (code := _token(raw))
        }
    )
    deliverables = _deliverable_descriptors(item.get("deliverables"))
    progress = {
        "contract": PROGRESS_CONTRACT,
        "version": PROGRESS_VERSION,
        "revision": _revision(item, snapshot, last_event_at),
        "overall_state": overall,
        "activity": activity,
        "active_stage": active_stage,
        "last_event_at": last_event_at,
        "stages": [stage_map[stage_id] for stage_id in _STAGE_IDS],
        "deliverables": deliverables,
        "deliverable_index": _deliverable_index(
            item.get("deliverable_index"), returned=len(deliverables)
        ),
        "final_report": final_report,
        "attention": attention,
        "milestones": milestones,
        "technical": {
            "run_id": _identifier(run.get("id")),
            "workflow_name": _identifier(run.get("workflow_name"), limit=96),
            "item_state": _token(item.get("status"), "draft"),
            "run_state": run_status or None,
            "recovery_count": _integer(run.get("recovery_count")),
            "event_count": _integer(
                snapshot.get("event_count"), len(_items(snapshot.get("events")))
            ),
            "checkpoint_count": _integer(
                snapshot.get("checkpoint_count"),
                len(_items(snapshot.get("checkpoints"))),
            ),
            "publication_count": _integer(
                snapshot.get("publication_count"),
                len(_items(snapshot.get("publications"))),
            ),
            "unknown_step_count": sum(
                1 for step in ordered_steps if _stage_for_step(step) is None
            ),
            "error_codes": error_codes,
        },
    }
    return progress


def compact_phase_summary(value: Any) -> list[dict[str, Any]]:
    """Keep the legacy phase shape without leaking provider configuration."""

    phases: list[dict[str, Any]] = []
    for raw_phase in _items(value)[:64]:
        phase = _record(raw_phase)
        if not phase:
            continue
        participants: list[dict[str, Any]] = []
        for raw_participant in _items(phase.get("participants"))[:24]:
            participant = _record(raw_participant)
            if not participant:
                continue
            participants.append(
                {
                    "status": _token(participant.get("status"), "unknown"),
                    "attempt_count": _integer(
                        participant.get("attempt_count"), maximum=1_000
                    ),
                }
            )
        phases.append(
            {
                "phase": _token(phase.get("phase")) or None,
                "status": _token(phase.get("status"), "pending"),
                "round": _integer(phase.get("round"), maximum=1_000),
                "started_at": _timestamp(phase.get("started_at")),
                "completed_at": _timestamp(phase.get("completed_at")),
                "participants": participants,
            }
        )
    return phases


def compact_list_item(item_value: Mapping[str, Any] | None) -> dict[str, Any]:
    item = _record(item_value)
    return {
        key: item.get(key)
        for key in ("id", "title", "status", "updated_at", "allowed_actions", "progress_summary")
    }


def compact_selected_item(item_value: Mapping[str, Any] | None) -> dict[str, Any]:
    item = _record(item_value)
    selected = {
        key: item.get(key)
        for key in (
            "id",
            "title",
            "task",
            "status",
            "preset",
            "safety_mode",
            "context_mode",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "archived_at",
            "allowed_actions",
            "progress",
            "progress_summary",
        )
    }
    selected["phases"] = compact_phase_summary(item.get("phases"))
    error_code = _token(item.get("error_code"))
    if error_code:
        selected["error_code"] = error_code
    return selected


def compact_preferences(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    preferences = _record(value)
    if not preferences:
        return None
    return {
        key: preferences.get(key)
        for key in (
            "safety_mode",
            "preset",
            "context_mode",
            "context7_policy",
            "role_profiles",
            "persisted",
            "non_git_confirmed",
        )
    }


def compact_execution_profiles(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose model choices without provider runners, sessions, or sandboxes."""

    profiles = _record(value)
    execution_profiles: dict[str, dict[str, Any]] = {}
    for raw_name, raw_profile in list(_record(profiles.get("execution_profiles")).items())[
        :100
    ]:
        name = _identifier(raw_name, limit=64)
        profile = _record(raw_profile)
        if not name or not profile:
            continue
        execution_profiles[name] = {
            "provider": _safe_text(profile.get("provider"), limit=64),
            "model": _safe_text(profile.get("model"), limit=128),
            "reasoning_effort": _safe_text(
                profile.get("reasoning_effort"), limit=64
            ),
            "agent": _safe_text(profile.get("agent"), limit=128),
            "effort": _safe_text(profile.get("effort"), limit=64),
            "enabled": bool(profile.get("enabled", True)),
            "description": _safe_text(profile.get("description"), limit=400),
        }

    roles: dict[str, dict[str, Any]] = {}
    for role in ("architect", "implementer", "reviewer"):
        raw_role = _record(_record(profiles.get("roles")).get(role))
        roles[role] = {
            "profiles": [
                name
                for raw_name in _items(raw_role.get("profiles"))[:24]
                if (name := _identifier(raw_name, limit=64)) is not None
            ],
            "strategy": _token(raw_role.get("strategy")) or None,
            "resolution": _token(raw_role.get("resolution")) or None,
        }

    resolved_roles: dict[str, list[dict[str, Any]]] = {}
    for role in ("architect", "implementer", "reviewer"):
        resolved: list[dict[str, Any]] = []
        for raw_profile in _items(_record(profiles.get("resolved_roles")).get(role))[
            :24
        ]:
            profile = _record(raw_profile)
            if not profile:
                continue
            resolved.append(
                {
                    "name": _identifier(profile.get("name"), limit=64),
                    "provider": _safe_text(profile.get("provider"), limit=64),
                    "model": _safe_text(profile.get("model"), limit=128),
                    "reasoning_effort": _safe_text(
                        profile.get("reasoning_effort"), limit=64
                    ),
                    "agent": _safe_text(profile.get("agent"), limit=128),
                    "effort": _safe_text(profile.get("effort"), limit=64),
                    "description": _safe_text(
                        profile.get("description"), limit=400
                    ),
                }
            )
        resolved_roles[role] = resolved

    return {
        "presets": [
            preset
            for raw_preset in _items(profiles.get("presets"))[:24]
            if (preset := _token(raw_preset))
        ],
        "execution_profiles": execution_profiles,
        "roles": roles,
        "resolved_roles": resolved_roles,
    }


def progress_summary(
    item_value: Mapping[str, Any] | None,
    progress_value: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the small, artifact-free summary used by the work-item list."""

    item = _record(item_value)
    progress = _record(progress_value)
    if progress:
        activity = _record(progress.get("activity"))
        return {
            "overall_state": _token(progress.get("overall_state"), "pending"),
            "activity": _safe_text(activity.get("message"), limit=160),
            "active_stage": _token(progress.get("active_stage")) or None,
            "last_event_at": _timestamp(progress.get("last_event_at")),
        }
    run = _record(item.get("run"))
    overall = _overall_state(item, run)
    run_status = _token(run.get("status"))
    activity = _fallback_activity(overall, None, run_status, {})
    if overall == "running":
        activity = {
            "kind": "working",
            "message": "Baldr está trabajando",
            "since": None,
            "evidence": "observed",
        }
    return {
        "overall_state": overall,
        "activity": activity["message"],
        "active_stage": None,
        "last_event_at": _last_timestamp(
            [run.get("updated_at"), item.get("updated_at")]
        ),
    }
