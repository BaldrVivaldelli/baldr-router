from __future__ import annotations

from typing import Any, Iterable


SUCCESS_STATUSES = {
    "planned",
    "implemented",
    "reviewed",
    "approved",
    "no_changes_needed",
}
BLOCKING_STATUSES = {"blocked", "needs_changes", "partial"}


def _report(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("final_report")
    return value if isinstance(value, dict) else {}


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _field(reports: list[dict[str, Any]], name: str) -> list[str]:
    return _unique(
        entry
        for report in reports
        for entry in (report.get(name) or [])
        if isinstance(entry, str)
    )


def _merge_decisions(reports: list[dict[str, Any]]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    values: dict[str, list[str]] = {}
    for report in reports:
        decisions = report.get("decisions") or {}
        if not isinstance(decisions, dict):
            continue
        for key, value in decisions.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if normalized_key and normalized_value:
                values.setdefault(normalized_key, []).append(normalized_value)
    merged: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for key, raw_values in values.items():
        unique = _unique(raw_values)
        if len(unique) == 1:
            merged[key] = unique[0]
        elif unique:
            conflicts.append({"key": key, "values": unique})
    return merged, conflicts


def _is_blocking(item: dict[str, Any]) -> bool:
    report = _report(item)
    status = str(report.get("status") or item.get("status") or "").lower()
    review_decision = str(report.get("review_decision") or "").lower()
    if review_decision in {"changes_required", "inconclusive"}:
        return True
    if report.get("blockers"):
        return True
    if status in BLOCKING_STATUSES or not bool(item.get("ok", True)):
        return True
    text = "\n".join(
        [
            *(report.get("blockers") or []),
            *(report.get("risks") or []),
            *(report.get("verification_needed") or []),
        ]
    ).lower()
    return any(
        marker in text
        for marker in ("blocker:", "[blocker]", "blocking:", "must fix:", "critical:")
    )


def _base_report(
    reports: list[dict[str, Any]], *, status: str, summary: str
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "files_modified": _field(reports, "files_modified"),
        "commands_run": _field(reports, "commands_run"),
        "tests_run": _field(reports, "tests_run"),
        "verification_needed": _field(reports, "verification_needed"),
        "risks": _field(reports, "risks"),
        "follow_up": _field(reports, "follow_up"),
        "decisions": _merge_decisions(reports)[0],
        "constraints": _field(reports, "constraints"),
        "assumptions": _field(reports, "assumptions"),
        "alternatives_rejected": _field(reports, "alternatives_rejected"),
        "acceptance_criteria": _field(reports, "acceptance_criteria"),
        "blockers": _field(reports, "blockers"),
    }


def reduce_phase(
    *,
    phase: str,
    participants: list[dict[str, Any]],
    policy: str,
    min_successes: int = 1,
    min_approvals: int = 1,
) -> dict[str, Any]:
    """Deterministically consolidate n/m/l phase participants.

    The reducer never invokes another model. It only combines the frozen
    structured reports and records explicit conflicts for operator review.
    """

    if not participants:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "No successful participants were available to reduce.",
            "participants": [],
            "resolution": {"policy": policy, "conflicts": ["no-participants"]},
        }

    reports = [_report(item) for item in participants]
    statuses = [str(report.get("status") or "").lower() for report in reports]
    blocking = [_is_blocking(item) for item in participants]
    summaries = [str(report.get("summary") or "").strip() for report in reports]
    conflicts: list[str] = []
    merged_decisions, decision_conflicts = _merge_decisions(reports)

    if any(blocking) and not all(blocking):
        conflicts.append("participants-disagree-on-blockers")
    nonempty_statuses = {status for status in statuses if status}
    if len(nonempty_statuses) > 1:
        conflicts.append("participants-returned-different-statuses")

    normalized_policy = (policy or "").strip().lower()
    if phase == "architect":
        normalized_policy = normalized_policy or "primary-with-advisors"
        if decision_conflicts:
            result_status = "blocked"
            ok = False
            conflicts.append("architecture-decision-conflict")
        elif normalized_policy == "unanimous" and conflicts:
            result_status = "blocked"
            ok = False
        elif normalized_policy == "conflict-blocks" and conflicts:
            result_status = "blocked"
            ok = False
        else:
            result_status = "planned"
            ok = True
        primary = summaries[0] if summaries else ""
        advisor_lines = [
            f"Advisor {index + 1}: {summary}"
            for index, summary in enumerate(summaries[1:])
            if summary
        ]
        summary = primary
        if advisor_lines:
            summary = "\n\n".join([primary, *advisor_lines]).strip()
    elif phase == "reviewer":
        normalized_policy = normalized_policy or "any-blocker"
        approvals = sum(
            1
            for item in participants
            if str(_report(item).get("review_decision") or "").lower() == "approved"
            or (
                not _report(item).get("review_decision")
                and not _is_blocking(item)
            )
        )
        if normalized_policy == "all-approved":
            approved = approvals == len(participants) and approvals >= min_approvals
        elif normalized_policy == "quorum":
            approved = approvals >= max(1, min_approvals)
        elif normalized_policy == "conflict-blocks":
            approved = not conflicts and approvals >= max(1, min_approvals)
        else:  # any-blocker
            approved = not any(blocking) and approvals >= max(1, min_approvals)
        result_status = "approved" if approved else "needs_changes"
        ok = True
        summary = "\n\n".join(summary for summary in summaries if summary)
    else:
        normalized_policy = normalized_policy or "first-success"
        result_status = statuses[0] or "implemented"
        ok = len(participants) >= max(1, min_successes)
        summary = summaries[0] if summaries else ""

    final_report = _base_report(reports, status=result_status, summary=summary)
    final_report["decisions"] = merged_decisions
    if phase == "reviewer":
        final_report["review_decision"] = (
            "approved" if result_status == "approved" else "changes_required"
        )
    return {
        "ok": ok,
        "status": result_status,
        "error_code": "architecture_conflict" if phase == "architect" and decision_conflicts else None,
        "participants": participants,
        "final_report": final_report,
        "resolution": {
            "policy": normalized_policy,
            "participant_count": len(participants),
            "min_successes": max(1, min_successes),
            "min_approvals": max(1, min_approvals),
            "conflicts": conflicts,
            "blocking_participants": sum(1 for value in blocking if value),
            "decision_conflicts": decision_conflicts,
        },
    }
