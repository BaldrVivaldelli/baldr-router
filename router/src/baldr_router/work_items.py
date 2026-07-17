from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ExecutionProfileConfig, load_config, save_config
from .durability.identity import workspace_identity
from .durability.store import DurableStore, utc_now_iso
from .execution_profiles import resolve_role_profiles
from .phase_deliverables import (
    PhaseDeliverableError,
    inspect_phase_deliverable,
    list_phase_deliverable_index_page,
    list_phase_deliverables,
    phase_deliverable_index_metadata,
)
from .platforming import normalize_path_for_runtime
from .work_item_progress import (
    compact_execution_profiles,
    compact_list_item,
    compact_preferences,
    compact_selected_item,
    progress_summary,
    project_work_item_progress,
)
from .workspace_policy import (
    WorkspacePolicyError,
    inspect_workspace,
    require_workspace,
    trust_workspace,
)

WORK_ITEM_STATUSES = {
    "draft",
    "ready",
    "running",
    "cancelling",
    "needs_attention",
    "completed",
    "failed",
    "cancelled",
    "archived",
}
TERMINAL_ITEM_STATUSES = {"completed", "failed", "cancelled", "archived"}
# ``automatic`` is the canonical default. The other values remain accepted so
# already-persisted items keep their exact execution semantics:
# ``worktree`` is the legacy isolated-Git mode, ``current`` works in place in a
# Git repository, and ``non-git`` is the explicitly confirmed unprotected mode.
SAFETY_MODES = {"automatic", "worktree", "current", "non-git"}
SAFETY_MODE_ALIASES = {"auto": "automatic"}
EXECUTION_PRESETS = {"fast", "balanced", "deep", "custom"}
CONTEXT_MODES = {"auto", "on", "off"}
ROLE_NAMES = ("architect", "implementer", "reviewer")
RECONCILIATION_ACTION_ORDER = (
    "authorize_changes",
    "decline_changes",
    "inspect_shadow",
    "continue_from_shadow",
    "apply_shadow_changes",
    "discard_shadow",
    "resume_from_checkpoint",
    "accept_existing_changes",
    "discard_worktree",
    "mark_failed",
)

_PUBLIC_WORK_ITEM_COLUMNS = """
    id, workspace_id, title, task_artifact_id, status, safety_mode, preset,
    context_mode, current_run_id, idempotency_key, revision, error_code,
    created_at, updated_at, started_at, completed_at, archived_at
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _safety_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return SAFETY_MODE_ALIASES.get(normalized, normalized)


def _workspace(path: str | Path) -> tuple[Path, dict[str, Any]]:
    root = normalize_path_for_runtime(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {root}")
    return root, workspace_identity(root)


def _title(task: str) -> str:
    first = next((line.strip() for line in task.splitlines() if line.strip()), "New Baldr item")
    first = re.sub(r"^[/#*\-\s]+", "", first).strip()
    return first[:96] or "New Baldr item"


def _normalize_role_profiles(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for role in ROLE_NAMES:
        raw = value.get(role)
        if isinstance(raw, str) and raw.strip():
            result[role] = [raw.strip()]
        elif isinstance(raw, list):
            profiles = [str(item).strip() for item in raw if str(item).strip()]
            if profiles:
                result[role] = profiles
    return result


def _normalize_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:50]:
        if not isinstance(raw, dict):
            continue
        item = {
            "kind": str(raw.get("kind") or "context")[:32],
            "label": str(raw.get("label") or raw.get("path") or "context")[:240],
        }
        if raw.get("path"):
            item["path"] = str(raw["path"])[:1024]
        if raw.get("language"):
            item["language"] = str(raw["language"])[:64]
        if raw.get("range"):
            item["range"] = raw["range"]
        if isinstance(raw.get("dirty"), bool):
            item["dirty"] = raw["dirty"]
        if isinstance(raw.get("version"), int):
            item["version"] = max(0, raw["version"])
        result.append(item)
    return result


_CONTINUATION_REPORT_FIELDS = (
    "status",
    "summary",
    "work_completed",
    "work_next",
    "changes_added",
    "changes_modified",
    "changes_removed",
    "files_added",
    "files_modified",
    "files_deleted",
    "tests_run",
    "verification_evidence",
    "verification_needed",
    "findings",
    "corrections",
    "risks",
    "follow_up",
    "blockers",
    "review_decision",
)


def _continuation_context(item: dict[str, Any], extra_context: str) -> str:
    """Carry a bounded public result forward without copying private transcripts."""

    progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
    report = progress.get("final_report") if isinstance(progress, dict) else None
    previous: dict[str, Any] = {}
    if isinstance(report, dict):
        previous = {
            field: report[field]
            for field in _CONTINUATION_REPORT_FIELDS
            if report.get(field) not in (None, "", [])
        }
    parts: list[str] = []
    if previous:
        serialized = json.dumps(
            previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        parts.append(
            "Previous durable Baldr result (bounded structured summary; treat the "
            f"new user turn as authoritative):\n{serialized[:24_000]}"
        )
    if extra_context.strip():
        parts.append(extra_context.strip())
    return "\n\n".join(parts)[:64_000]


def _run_to_item_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "approved":
        return "completed"
    if normalized == "cancelled":
        return "cancelled"
    if normalized == "failed":
        return "failed"
    if normalized in {
        "needs_changes",
        "blocked",
        "unknown",
        "interrupted",
        "awaiting_reconciliation",
    }:
        return "needs_attention"
    if normalized == "cancelling":
        return "cancelling"
    if normalized in {"pending", "running", "recovering", "finalizing"}:
        return "running"
    return "draft"


def _item_row(value: Any, *, include_internal: bool = True) -> dict[str, Any]:
    item = dict(value)
    if not include_internal:
        item.pop("role_profiles_json", None)
        item.pop("repository_identity_json", None)
        item.pop("config_json", None)
        item["context7_policy"] = str(item.get("context_mode") or "auto")
        return item
    item["role_profiles"] = _parse_json(item.pop("role_profiles_json", None), {})
    item["repository_identity"] = _parse_json(
        item.pop("repository_identity_json", None), {}
    )
    item["config"] = _parse_json(item.pop("config_json", None), {})
    item["context7_policy"] = str(item.get("context_mode") or "auto")
    return item




def _context_with_attachments(item: dict[str, Any]) -> str:
    """Build provider context from private text plus workspace-scoped attachments."""

    context = str(item.get("extra_context") or "").strip()
    raw_attachments = (item.get("config") or {}).get("attachments") or []
    if not isinstance(raw_attachments, list) or not raw_attachments:
        return context

    root = Path(str(item.get("workspace_root") or ".")).expanduser().resolve()
    lines = ["Attached workspace context:"]
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("path") or "context")
        path_value = str(raw.get("path") or "").strip()
        display_path = ""
        if path_value:
            try:
                candidate = normalize_path_for_runtime(path_value).expanduser().resolve()
                display_path = candidate.relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError):
                # Never instruct a provider to read outside the authorized workspace.
                display_path = ""
        suffix = f" ({display_path})" if display_path and display_path != label else ""
        lines.append(f"- {label}{suffix}")

    attachment_context = "\n".join(lines) if len(lines) > 1 else ""
    return "\n\n".join(part for part in (context, attachment_context) if part)


def _phase_summary(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    phases: list[dict[str, Any]] = []
    for step in snapshot.get("steps", []):
        participants = []
        for participant in step.get("participants", []):
            participants.append(
                {
                    "profile": participant.get("profile_name"),
                    "provider": participant.get("provider"),
                    "model": participant.get("model"),
                    "agent": participant.get("agent"),
                    "status": participant.get("status"),
                    "attempt_count": participant.get("attempt_count", 0),
                }
            )
        phases.append(
            {
                "id": step.get("id"),
                "key": step.get("step_key"),
                "phase": step.get("phase"),
                "status": step.get("status"),
                "round": step.get("round_number", 0),
                "started_at": step.get("started_at"),
                "completed_at": step.get("completed_at"),
                "participants": participants,
            }
        )
    return phases


def _allowed_actions(item: dict[str, Any], snapshot: dict[str, Any] | None) -> list[str]:
    status = str(item.get("status") or "draft")
    if status == "archived":
        return ["restore", "delete"]
    actions: list[str] = []
    if status in {"draft", "ready", "failed", "cancelled"}:
        actions.append("start")
    if status in {"running", "cancelling"}:
        actions.append("cancel")
    run_status = str(((snapshot or {}).get("run") or {}).get("status") or "")
    if run_status == "awaiting_reconciliation":
        run = (snapshot or {}).get("run") or {}
        reconciliation = run.get("reconciliation") or {}
        raw_actions = reconciliation.get("allowed_actions")
        if isinstance(raw_actions, list):
            recorded = {str(action) for action in raw_actions}
        elif item.get("safety_mode") == "non-git":
            # Legacy non-Git runs predate recorded action capabilities. They
            # may keep the current files, but never claim a restorable backup.
            recorded = {"accept_existing_changes", "mark_failed"}
        else:
            recorded = {"mark_failed"}
        if _legacy_write_policy_failure(snapshot):
            # Runs created before write authorization existed reported the
            # architect's read-only boundary as a blocker.  Offer the equivalent
            # durable decision so those sessions can resume without being lost.
            recorded.update({"authorize_changes", "decline_changes"})
        actions.extend(
            action for action in RECONCILIATION_ACTION_ORDER if action in recorded
        )
    elif status == "needs_attention":
        actions.append("start")
    if status in {"completed", "failed", "cancelled"} or (
        status == "needs_attention"
        and bool(snapshot)
        and run_status != "awaiting_reconciliation"
    ):
        actions.append("continue")
    if status not in {"running", "cancelling", "archived"}:
        actions.append("archive")
    return list(dict.fromkeys(actions))


def _legacy_write_policy_failure(snapshot: dict[str, Any] | None) -> bool:
    run = (snapshot or {}).get("run") or {}
    if str(run.get("error_code") or "") not in {
        "workflow_phase_failed",
        "phase_report_blocked",
    }:
        return False
    for step in (snapshot or {}).get("steps") or []:
        if str(step.get("phase") or "") != "architect":
            continue
        output = step.get("output") or {}
        text = _json(output).lower()
        if any(
            marker in text
            for marker in (
                "regla de no modificar archivos",
                "bloqueada por la regla de no modificar",
                "read-only planning boundary",
                "rule not to modify files",
            )
        ):
            return True
    return False


def workbench_options() -> dict[str, Any]:
    return {
        "safety_modes": [
            {
                "id": "automatic",
                "label": "Pedir autorización",
                "description": "Recomendada y predeterminada: Baldr planifica en solo lectura y te pregunta antes de modificar esta carpeta.",
                "recommended": True,
                "default": True,
            },
            {
                "id": "current",
                "label": "Trabajar directamente",
                "description": "Permite cambios directos sin una pausa de autorización por tarea y usa Git para revisarlos.",
            },
            {
                "id": "non-git",
                "label": "Sin protección",
                "description": "Trabaja directamente, sin exigir Git y sin recuperación automática.",
                "requires_confirmation": True,
            },
        ],
        "presets": [
            {"id": "fast", "label": "Rápido", "description": "Resuelve tareas simples con menos análisis."},
            {"id": "balanced", "label": "Estándar", "description": "Equilibra rapidez y profundidad."},
            {"id": "deep", "label": "Detallado", "description": "Dedica más análisis a trabajos complejos."},
            {"id": "custom", "label": "A medida", "description": "Usa el equipo elegido para cada etapa."},
        ],
        "context_modes": [
            {"id": "auto", "label": "Ayuda automática"},
            {"id": "on", "label": "Ayuda activa"},
            {"id": "off", "label": "Ayuda desactivada"},
        ],
        "slash_commands": [
            {"id": "setup", "usage": "/setup", "description": "Abrir las opciones de Baldr."},
            {"id": "new", "usage": "/new <tarea>", "description": "Guardar una sesión para después."},
            {"id": "run", "usage": "/run [tarea]", "description": "Empezar la sesión elegida o crear una nueva."},
            {"id": "status", "usage": "/status", "description": "Actualizar el estado de las sesiones."},
            {"id": "profile", "usage": "/profile <fast|balanced|deep|custom>", "description": "Cambiar el nivel de detalle."},
            {"id": "git", "usage": "/git <automatic|current|off>", "description": "Elegí cómo proteger los cambios de esta carpeta."},
            {"id": "context", "usage": "/context <auto|on|off>", "description": "Configurar la ayuda adicional."},
            {"id": "roles", "usage": "/roles", "description": "Elegir el equipo para cada etapa."},
            {"id": "cancel", "usage": "/cancel", "description": "Detener la sesión en curso."},
            {"id": "resume", "usage": "/resume", "description": "Continuar una sesión interrumpida."},
            {"id": "archive", "usage": "/archive", "description": "Archivar la sesión elegida."},
            {"id": "restore", "usage": "/restore", "description": "Restaurar la sesión archivada elegida."},
            {"id": "delete", "usage": "/delete", "description": "Eliminar permanentemente la sesión archivada elegida."},
            {"id": "help", "usage": "/help", "description": "Ver los comandos disponibles."},
        ],
    }


class WorkItemService:
    """Durable user-facing items backed by the shared workflow state store."""

    def __init__(self, store: DurableStore | None = None) -> None:
        self.store = store or DurableStore()

    def _event(
        self,
        connection: Any,
        item_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO work_item_events(item_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, event_type, _json(payload or {}), utc_now_iso()),
        )

    def _insert_turn(
        self,
        connection: Any,
        *,
        item_id: str,
        ordinal: int,
        item_revision: int,
        request_artifact_id: str,
        context_artifact_id: str | None,
        source: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO work_item_turns(
                id, item_id, ordinal, item_revision, request_artifact_id,
                context_artifact_id, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"wit-{uuid.uuid4().hex}",
                item_id,
                ordinal,
                item_revision,
                request_artifact_id,
                context_artifact_id,
                (source.strip() or "unknown")[:64],
                utc_now_iso(),
            ),
        )

    def _load_turns(
        self, item_id: str, *, include_internal: bool
    ) -> list[dict[str, Any]]:
        limit = 500 if include_internal else 100
        rows = self.store.connect().execute(
            """
            SELECT id, ordinal, item_revision, request_artifact_id,
                   context_artifact_id, run_id, source, created_at
            FROM work_item_turns WHERE item_id=?
            ORDER BY ordinal DESC LIMIT ?
            """,
            (item_id, limit),
        ).fetchall()
        turns: list[dict[str, Any]] = []
        for row in reversed(rows):
            value = dict(row)
            request_artifact_id = value.pop("request_artifact_id", None)
            context_artifact_id = value.pop("context_artifact_id", None)
            request = (
                self.store.load_artifact(request_artifact_id)
                if include_internal
                else self.store.load_public_text_artifact(request_artifact_id)
            )
            value["request"] = str(request or "")
            value["has_context"] = bool(context_artifact_id)
            if include_internal:
                value["context"] = str(
                    self.store.load_artifact(context_artifact_id) or ""
                )
                value["request_artifact_id"] = request_artifact_id
                value["context_artifact_id"] = context_artifact_id
            turns.append(value)
        return turns

    def _load_item(
        self,
        row: Any,
        *,
        include_task: bool = True,
        include_internal: bool = True,
    ) -> dict[str, Any]:
        item = _item_row(row, include_internal=include_internal)
        if include_task:
            if include_internal:
                item["task"] = str(
                    self.store.load_artifact(item.get("task_artifact_id")) or ""
                )
                item["extra_context"] = str(
                    self.store.load_artifact(item.get("extra_context_artifact_id"))
                    or ""
                )
            else:
                item["task"] = self.store.load_public_text_artifact(
                    item.get("task_artifact_id")
                )
        return item

    def preferences(self, workspace_root: str | Path) -> dict[str, Any]:
        root, identity = _workspace(workspace_root)
        return self._preferences_for_identity(root, identity)

    def _preferences_for_identity(
        self, root: Path, identity: dict[str, Any]
    ) -> dict[str, Any]:
        row = self.store.connect().execute(
            "SELECT * FROM workspace_preferences WHERE workspace_id = ?",
            (identity["workspace_id"],),
        ).fetchone()
        if row is None:
            cfg = load_config()
            return {
                "workspace_id": identity["workspace_id"],
                "workspace_root": str(root),
                "safety_mode": "automatic",
                "preset": "balanced",
                "context_mode": "auto",
                "context7_policy": "auto",
                "role_profiles": {
                    role: list(cfg.roles[role].profiles) for role in ROLE_NAMES
                },
                "persisted": False,
                "non_git_confirmed": bool(
                    inspect_workspace(root, access="read").get("intentional_non_git")
                ),
            }
        value = dict(row)
        value["role_profiles"] = _parse_json(value.pop("role_profiles_json", None), {})
        value["context7_policy"] = str(value.get("context_mode") or "auto")
        value["persisted"] = True
        value["non_git_confirmed"] = bool(
            inspect_workspace(root, access="read").get("intentional_non_git")
        )
        return value

    def _validate_profiles(self, role_profiles: dict[str, list[str]]) -> None:
        cfg = load_config()
        unknown = sorted(
            {
                profile
                for profiles in role_profiles.values()
                for profile in profiles
                if profile not in cfg.execution_profiles
            }
        )
        if unknown:
            raise ValueError(f"Unknown execution profiles: {', '.join(unknown)}")

    def set_preferences(
        self,
        workspace_root: str | Path,
        *,
        safety_mode: str | None = None,
        preset: str | None = None,
        context_mode: str | None = None,
        context7_policy: str | None = None,
        role_profiles: dict[str, list[str]] | None = None,
        allow_non_git: bool = False,
    ) -> dict[str, Any]:
        root, identity = _workspace(workspace_root)
        current = self.preferences(root)
        selected_safety = _safety_mode(safety_mode or current["safety_mode"])
        selected_preset = str(preset or current["preset"]).strip().lower()
        selected_context = str(
            context7_policy or context_mode or current["context_mode"]
        ).strip().lower()
        selected_roles = _normalize_role_profiles(role_profiles or current.get("role_profiles"))
        if selected_safety not in SAFETY_MODES:
            raise ValueError(f"Invalid safety mode: {selected_safety}")
        if selected_preset not in EXECUTION_PRESETS:
            raise ValueError(f"Invalid execution preset: {selected_preset}")
        if selected_context not in CONTEXT_MODES:
            raise ValueError(f"Invalid Context7 mode: {selected_context}")
        self._validate_profiles(selected_roles)

        protected_non_git = selected_safety == "automatic"
        inspection = inspect_workspace(
            root,
            access="write",
            protected_non_git=protected_non_git,
        )
        if selected_safety == "non-git":
            if not inspection.get("intentional_non_git") and not allow_non_git:
                raise WorkspacePolicyError(
                    "Non-Git mode requires explicit client/user consent.",
                    code="workspace_non_git_confirmation_required",
                    details=inspection,
                )
            trust_result = trust_workspace(root, force=True)
        elif selected_safety == "automatic":
            trust_result = (
                {"ok": True}
                if inspection.get("ok")
                else trust_workspace(root, protected_non_git=True)
            )
        else:
            trust_result = (
                {"ok": True}
                if inspection.get("ok")
                else trust_workspace(root, force=False)
            )
        if not trust_result.get("ok"):
            error = trust_result.get("error") or {}
            raise WorkspacePolicyError(
                str(trust_result.get("reason") or "Workspace trust failed."),
                code=str(error.get("code") or "workspace_trust_failed"),
                details=trust_result,
            )

        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO workspace_preferences(
                    workspace_id, workspace_root, safety_mode, preset, context_mode,
                    role_profiles_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    workspace_root = excluded.workspace_root,
                    safety_mode = excluded.safety_mode,
                    preset = excluded.preset,
                    context_mode = excluded.context_mode,
                    role_profiles_json = excluded.role_profiles_json,
                    updated_at = excluded.updated_at
                """,
                (
                    identity["workspace_id"],
                    str(root),
                    selected_safety,
                    selected_preset,
                    selected_context,
                    _json(selected_roles),
                    now,
                    now,
                ),
            )
        return self.preferences(root)

    def create(
        self,
        *,
        workspace_root: str | Path,
        task: str,
        title: str | None = None,
        extra_context: str = "",
        attachments: list[dict[str, Any]] | None = None,
        safety_mode: str | None = None,
        preset: str | None = None,
        context_mode: str | None = None,
        context7_policy: str | None = None,
        role_profiles: dict[str, list[str]] | None = None,
        config: dict[str, Any] | None = None,
        allow_non_git: bool = False,
        source: str = "create",
    ) -> dict[str, Any]:
        clean_task = task.strip()
        if not clean_task:
            raise ValueError("A work item task must not be empty.")
        root, identity = _workspace(workspace_root)
        defaults = self.preferences(root)
        safety = _safety_mode(safety_mode or defaults["safety_mode"])
        selected_preset = str(preset or defaults["preset"]).strip().lower()
        context = str(
            context7_policy or context_mode or defaults["context_mode"]
        ).strip().lower()
        roles = _normalize_role_profiles(role_profiles or defaults.get("role_profiles"))
        if safety not in SAFETY_MODES:
            raise ValueError(f"Invalid safety mode: {safety}")
        if selected_preset not in EXECUTION_PRESETS:
            raise ValueError(f"Invalid execution preset: {selected_preset}")
        if context not in CONTEXT_MODES:
            raise ValueError(f"Invalid Context7 mode: {context}")
        self._validate_profiles(roles)
        if safety == "non-git" and allow_non_git:
            inspection = inspect_workspace(root, access="write")
            if not inspection.get("intentional_non_git"):
                trust_result = trust_workspace(root, force=True)
                if not trust_result.get("ok"):
                    raise WorkspacePolicyError(
                        str(trust_result.get("reason") or "Workspace trust failed."),
                        code=str((trust_result.get("error") or {}).get("code") or "workspace_trust_failed"),
                        details=trust_result,
                    )
        # A draft can be materialized before Non-Git consent. Provider access
        # remains blocked by ``start`` until the user explicitly confirms the
        # reduced-guarantee mode. This keeps typed task text durable while the
        # rich client presents a small guided choice.

        item_id = f"wi-{uuid.uuid4().hex}"
        revision = 1
        idempotency_key = f"work-item:{item_id}:r{revision}"
        now = utc_now_iso()
        metadata = dict(config or {})
        metadata["attachments"] = _normalize_attachments(attachments)
        with self.store.transaction(immediate=True) as connection:
            task_artifact_id = self.store._insert_artifact(
                connection,
                run_id=None,
                kind="work-item-task-private",
                value=clean_task,
                media_type="text/plain",
                redaction_level="private",
                redact=False,
            )
            extra_artifact_id = None
            if extra_context.strip():
                extra_artifact_id = self.store._insert_artifact(
                    connection,
                    run_id=None,
                    kind="work-item-context-private",
                    value=extra_context,
                    media_type="text/plain",
                    redaction_level="private",
                    redact=False,
                )
            connection.execute(
                """
                INSERT INTO work_items(
                    id, workspace_id, workspace_root, repository_identity_json,
                    title, task_artifact_id, extra_context_artifact_id, status,
                    safety_mode, preset, context_mode, role_profiles_json, config_json,
                    idempotency_key, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    identity["workspace_id"],
                    str(root),
                    _json(identity),
                    (title or _title(clean_task)).strip()[:160],
                    task_artifact_id,
                    extra_artifact_id,
                    safety,
                    selected_preset,
                    context,
                    _json(roles),
                    _json(metadata),
                    idempotency_key,
                    revision,
                    now,
                    now,
                ),
            )
            self._insert_turn(
                connection,
                item_id=item_id,
                ordinal=1,
                item_revision=revision,
                request_artifact_id=task_artifact_id,
                context_artifact_id=extra_artifact_id,
                source=source,
            )
            self._event(
                connection,
                item_id,
                "work_item.created",
                {
                    "status": "draft",
                    "safety_mode": safety,
                    "preset": selected_preset,
                    "context_mode": context,
                },
            )
        return self.get(item_id)

    def continue_item(
        self,
        item_id: str,
        *,
        workspace_root: str | Path,
        request: str,
        extra_context: str = "",
        attachments: list[dict[str, Any]] | None = None,
        source: str = "generic-mcp",
    ) -> dict[str, Any]:
        """Append one immutable user turn and prepare a new run revision."""

        current = self.get(item_id, include_timeline=False)
        _root, identity = _workspace(workspace_root)
        if str(current.get("workspace_id")) != str(identity["workspace_id"]):
            raise ValueError("The work item does not belong to this workspace.")
        if current["status"] in {"running", "cancelling", "archived"}:
            raise ValueError(f"A {current['status']} work item cannot be continued.")
        run_status = str((current.get("run") or {}).get("status") or "")
        if run_status == "awaiting_reconciliation":
            raise ValueError(
                "This item requires reconciliation before the conversation can continue."
            )
        clean_request = request.strip()
        if not clean_request:
            raise ValueError("A continuation request must not be empty.")

        revision = int(current["revision"]) + 1
        context = _continuation_context(current, extra_context)
        metadata = dict(current.get("config") or {})
        metadata["attachments"] = _normalize_attachments(attachments)
        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            # Legacy work items predate the turn table. Preserve their original
            # immutable task before appending the first real continuation.
            turn_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_item_turns WHERE item_id=?", (item_id,)
                ).fetchone()[0]
            )
            if turn_count == 0:
                self._insert_turn(
                    connection,
                    item_id=item_id,
                    ordinal=1,
                    item_revision=int(current["revision"]),
                    request_artifact_id=str(current["task_artifact_id"]),
                    context_artifact_id=current.get("extra_context_artifact_id"),
                    source="legacy",
                )
                turn_count = 1
            request_artifact_id = self.store._insert_artifact(
                connection,
                run_id=None,
                kind="work-item-turn-private",
                value=clean_request,
                media_type="text/plain",
                redaction_level="private",
                redact=False,
            )
            context_artifact_id = None
            if context:
                context_artifact_id = self.store._insert_artifact(
                    connection,
                    run_id=None,
                    kind="work-item-turn-context-private",
                    value=context,
                    media_type="text/plain",
                    redaction_level="private",
                    redact=False,
                )
            connection.execute(
                """
                UPDATE work_items
                SET task_artifact_id=?, extra_context_artifact_id=?, config_json=?,
                    revision=?, idempotency_key=?, status='draft', current_run_id=NULL,
                    error_code=NULL, error_reason=NULL, completed_at=NULL,
                    archived_at=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    request_artifact_id,
                    context_artifact_id,
                    _json(metadata),
                    revision,
                    f"work-item:{item_id}:r{revision}",
                    now,
                    item_id,
                ),
            )
            self._insert_turn(
                connection,
                item_id=item_id,
                ordinal=turn_count + 1,
                item_revision=revision,
                request_artifact_id=request_artifact_id,
                context_artifact_id=context_artifact_id,
                source=source,
            )
            self._event(
                connection,
                item_id,
                "work_item.turn_appended",
                {"ordinal": turn_count + 1, "revision": revision, "source": source[:64]},
            )
        return self.get(item_id)

    def update(
        self,
        item_id: str,
        *,
        title: str | None = None,
        task: str | None = None,
        extra_context: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        safety_mode: str | None = None,
        preset: str | None = None,
        context_mode: str | None = None,
        context7_policy: str | None = None,
        role_profiles: dict[str, list[str]] | None = None,
        config: dict[str, Any] | None = None,
        allow_non_git: bool = False,
    ) -> dict[str, Any]:
        current = self.get(item_id, include_timeline=False)
        if current["status"] in {"running", "cancelling"}:
            raise ValueError("A running work item cannot be edited.")
        selected_safety = _safety_mode(safety_mode or current["safety_mode"])
        selected_preset = str(preset or current["preset"]).strip().lower()
        selected_context = str(
            context7_policy or context_mode or current["context_mode"]
        ).strip().lower()
        selected_roles = _normalize_role_profiles(role_profiles or current["role_profiles"])
        if selected_safety not in SAFETY_MODES:
            raise ValueError(f"Invalid safety mode: {selected_safety}")
        if selected_preset not in EXECUTION_PRESETS:
            raise ValueError(f"Invalid execution preset: {selected_preset}")
        if selected_context not in CONTEXT_MODES:
            raise ValueError(f"Invalid Context7 mode: {selected_context}")
        self._validate_profiles(selected_roles)
        if selected_safety == "non-git" and allow_non_git:
            inspection = inspect_workspace(current["workspace_root"], access="write")
            if not inspection.get("intentional_non_git"):
                trust_result = trust_workspace(current["workspace_root"], force=True)
                if not trust_result.get("ok"):
                    raise WorkspacePolicyError(
                        str(trust_result.get("reason") or "Workspace trust failed."),
                        code=str((trust_result.get("error") or {}).get("code") or "workspace_trust_failed"),
                        details=trust_result,
                    )

        clean_task = (task if task is not None else current["task"]).strip()
        clean_context = (
            extra_context if extra_context is not None else current.get("extra_context", "")
        )
        if not clean_task:
            raise ValueError("A work item task must not be empty.")
        metadata = dict(current.get("config") or {})
        if config:
            metadata.update(config)
        if attachments is not None:
            metadata["attachments"] = _normalize_attachments(attachments)
        execution_changed = any(
            [
                task is not None and clean_task != current["task"],
                extra_context is not None and clean_context != current.get("extra_context", ""),
                selected_safety != current["safety_mode"],
                selected_preset != current["preset"],
                selected_context != current["context_mode"],
                selected_roles != current["role_profiles"],
            ]
        )
        revision = int(current["revision"]) + (1 if execution_changed else 0)
        idempotency_key = f"work-item:{item_id}:r{revision}"
        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            task_artifact_id = current["task_artifact_id"]
            if task is not None and clean_task != current["task"]:
                task_artifact_id = self.store._insert_artifact(
                    connection,
                    run_id=None,
                    kind="work-item-task-private",
                    value=clean_task,
                    media_type="text/plain",
                    redaction_level="private",
                    redact=False,
                )
            extra_artifact_id = current.get("extra_context_artifact_id")
            if extra_context is not None and clean_context != current.get("extra_context", ""):
                extra_artifact_id = None
                if clean_context.strip():
                    extra_artifact_id = self.store._insert_artifact(
                        connection,
                        run_id=None,
                        kind="work-item-context-private",
                        value=clean_context,
                        media_type="text/plain",
                        redaction_level="private",
                        redact=False,
                    )
            connection.execute(
                """
                UPDATE work_items
                SET title=?, task_artifact_id=?, extra_context_artifact_id=?,
                    safety_mode=?, preset=?, context_mode=?, role_profiles_json=?,
                    config_json=?, revision=?, idempotency_key=?,
                    status=CASE WHEN ? THEN 'draft' ELSE status END,
                    current_run_id=CASE WHEN ? THEN NULL ELSE current_run_id END,
                    error_code=CASE WHEN ? THEN NULL ELSE error_code END,
                    error_reason=CASE WHEN ? THEN NULL ELSE error_reason END,
                    completed_at=CASE WHEN ? THEN NULL ELSE completed_at END,
                    archived_at=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    (title or current["title"]).strip()[:160],
                    task_artifact_id,
                    extra_artifact_id,
                    selected_safety,
                    selected_preset,
                    selected_context,
                    _json(selected_roles),
                    _json(metadata),
                    revision,
                    idempotency_key,
                    int(execution_changed),
                    int(execution_changed),
                    int(execution_changed),
                    int(execution_changed),
                    int(execution_changed),
                    now,
                    item_id,
                ),
            )
            self._event(
                connection,
                item_id,
                "work_item.updated",
                {
                    "execution_changed": execution_changed,
                    "revision": revision,
                    "safety_mode": selected_safety,
                    "preset": selected_preset,
                    "context_mode": selected_context,
                },
            )
        return self.get(item_id)

    def _link_run(self, connection: Any, item_id: str, run_id: str) -> None:
        existing = connection.execute(
            "SELECT 1 FROM work_item_runs WHERE item_id=? AND run_id=?",
            (item_id, run_id),
        ).fetchone()
        if existing is None:
            item_row = connection.execute(
                "SELECT revision FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            turn_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_item_turns WHERE item_id=?", (item_id,)
                ).fetchone()[0]
            )
            ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(value), 0) + 1 FROM (
                        SELECT ordinal AS value FROM work_item_runs WHERE item_id=?
                        UNION ALL
                        SELECT run_ordinal AS value FROM phase_deliverables WHERE work_item_id=?
                    )
                    """,
                    (item_id, item_id),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO work_item_runs(item_id, run_id, ordinal, relation, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    run_id,
                    ordinal,
                    "follow-up" if turn_count > 1 else "primary",
                    utc_now_iso(),
                ),
            )
            if item_row is not None:
                connection.execute(
                    """
                    UPDATE work_item_turns SET run_id=?
                    WHERE item_id=? AND item_revision=? AND run_id IS NULL
                    """,
                    (run_id, item_id, int(item_row["revision"])),
                )

    def _sync(
        self, item: dict[str, Any], *, include_internal: bool = True
    ) -> dict[str, Any]:
        if item.get("status") == "archived":
            return item
        run = None
        if item.get("current_run_id"):
            run = (
                self.store.get_run(str(item["current_run_id"]))
                if include_internal
                else self.store.get_run_public(str(item["current_run_id"]))
            )
        if run is None:
            run = (
                self.store.get_run_by_idempotency_key(str(item["idempotency_key"]))
                if include_internal
                else self.store.get_run_by_idempotency_key_public(
                    str(item["idempotency_key"])
                )
            )
        if run is None:
            return item
        desired = _run_to_item_status(run.get("status"))
        changed = (
            item.get("current_run_id") != run.get("id")
            or item.get("status") != desired
            or item.get("error_code") != run.get("error_code")
            or (
                include_internal
                and item.get("error_reason") != run.get("error_reason")
            )
        )
        if changed and not include_internal:
            # Public status polling is observational.  Reflect the durable run
            # in memory without turning every refresh into a work-item write.
            item = {
                **item,
                "current_run_id": run.get("id"),
                "status": desired,
                "error_code": run.get("error_code"),
                "updated_at": run.get("updated_at") or item.get("updated_at"),
            }
            if desired in TERMINAL_ITEM_STATUSES and not item.get("completed_at"):
                item["completed_at"] = run.get("completed_at") or run.get(
                    "updated_at"
                )
            changed = False
        if changed:
            terminal = desired in TERMINAL_ITEM_STATUSES
            now = utc_now_iso()
            with self.store.transaction(immediate=True) as connection:
                self._link_run(connection, item["id"], str(run["id"]))
                if include_internal:
                    connection.execute(
                        """
                        UPDATE work_items
                        SET current_run_id=?, status=?, error_code=?, error_reason=?, updated_at=?,
                            completed_at=CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE completed_at END
                        WHERE id=?
                        """,
                        (
                            run.get("id"),
                            desired,
                            run.get("error_code"),
                            run.get("error_reason"),
                            now,
                            int(terminal),
                            now,
                            item["id"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE work_items
                        SET current_run_id=?, status=?, error_code=?, updated_at=?,
                            completed_at=CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE completed_at END
                        WHERE id=?
                        """,
                        (
                            run.get("id"),
                            desired,
                            run.get("error_code"),
                            now,
                            int(terminal),
                            now,
                            item["id"],
                        ),
                    )
                self._event(
                    connection,
                    item["id"],
                    "work_item.synced",
                    {"run_id": run.get("id"), "run_status": run.get("status"), "status": desired},
                )
            row = self.store.connect().execute(
                "SELECT * FROM work_items WHERE id=?", (item["id"],)
            ).fetchone()
            assert row is not None
            item = self._load_item(
                row,
                include_task="task" in item,
                include_internal=include_internal,
            )
        item["run"] = run
        return item

    def list(
        self,
        *,
        workspace_root: str | Path | None = None,
        limit: int = 100,
        include_archived: bool = False,
        include_internal: bool = True,
        _workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_root is not None:
            clauses.append("workspace_id=?")
            if _workspace_id is None:
                _, identity = _workspace(workspace_root)
                _workspace_id = str(identity["workspace_id"])
            params.append(_workspace_id)
        if not include_archived:
            clauses.append("archived_at IS NULL")
        query = (
            "SELECT * FROM work_items"
            if include_internal
            else f"SELECT {_PUBLIC_WORK_ITEM_COLUMNS} FROM work_items"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = self.store.connect().execute(query, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._sync(
                self._load_item(
                    row, include_task=False, include_internal=include_internal
                ),
                include_internal=include_internal,
            )
            item["allowed_actions"] = _allowed_actions(item, None)
            item["progress_summary"] = progress_summary(item)
            items.append(item)
        return items

    def get(
        self,
        item_id: str,
        *,
        include_timeline: bool = True,
        include_internal: bool = True,
    ) -> dict[str, Any]:
        query = (
            "SELECT * FROM work_items WHERE id=?"
            if include_internal
            else f"SELECT {_PUBLIC_WORK_ITEM_COLUMNS} FROM work_items WHERE id=?"
        )
        row = self.store.connect().execute(query, (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        item = self._sync(
            self._load_item(row, include_internal=include_internal),
            include_internal=include_internal,
        )
        snapshot: dict[str, Any] | None = None
        if item.get("current_run_id"):
            try:
                snapshot = (
                    self.store.snapshot_run(str(item["current_run_id"]))
                    if include_internal
                    else self.store.snapshot_run_public(str(item["current_run_id"]))
                )
            except KeyError:
                snapshot = None
        item["phases"] = _phase_summary(snapshot)
        item["allowed_actions"] = _allowed_actions(item, snapshot)
        item["deliverables"] = list_phase_deliverables(
            self.store,
            work_item_id=item_id,
            workspace_id=str(item["workspace_id"]),
        )
        item["deliverable_index"] = phase_deliverable_index_metadata(
            self.store,
            work_item_id=item_id,
            workspace_id=str(item["workspace_id"]),
            returned=len(item["deliverables"]),
        )
        item["progress"] = project_work_item_progress(item, snapshot)
        item["progress_summary"] = progress_summary(item, item["progress"])
        item["turns"] = self._load_turns(
            item_id, include_internal=include_internal
        )
        if snapshot and include_internal:
            item["workflow"] = {
                "run": snapshot.get("run"),
                "steps": snapshot.get("steps"),
                "checkpoints": snapshot.get("checkpoints"),
                "events": snapshot.get("events"),
            }
        if include_timeline and include_internal:
            timeline: list[dict[str, Any]] = []
            for event in self.store.connect().execute(
                "SELECT * FROM work_item_events WHERE item_id=? ORDER BY sequence",
                (item_id,),
            ).fetchall():
                value = dict(event)
                value["source"] = "work-item"
                value["payload"] = _parse_json(value.pop("payload_json", None), {})
                timeline.append(value)
            if snapshot:
                for event in snapshot.get("events", []):
                    timeline.append({**event, "source": "workflow"})
            timeline.sort(
                key=lambda entry: (
                    str(entry.get("created_at") or ""),
                    int(entry.get("sequence") or 0),
                )
            )
            item["timeline"] = timeline[-200:]
        return item if include_internal else compact_selected_item(item)

    def inspect_phase(
        self,
        item_id: str,
        *,
        workspace_root: str | Path,
        stage: str,
        round_number: int,
        run_ordinal: int | None = None,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Read one bounded page without accepting an artifact or database ID."""

        _root, identity = _workspace(workspace_root)
        workspace_id = str(identity["workspace_id"])
        row = self.store.connect().execute(
            "SELECT workspace_id FROM work_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None or str(row["workspace_id"]) != workspace_id:
            # Keep cross-workspace and unknown-item responses indistinguishable.
            raise PhaseDeliverableError(
                "phase_deliverable_not_found",
                "No phase deliverable exists for this task and workspace.",
            )
        return inspect_phase_deliverable(
            self.store,
            work_item_id=item_id,
            workspace_id=workspace_id,
            stage=stage,
            round_number=round_number,
            run_ordinal=run_ordinal,
            cursor=cursor,
            page_size=page_size,
        )

    def list_deliverables(
        self,
        item_id: str,
        *,
        workspace_root: str | Path,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Page descriptor history without reading any deliverable document."""

        _root, identity = _workspace(workspace_root)
        workspace_id = str(identity["workspace_id"])
        row = self.store.connect().execute(
            "SELECT workspace_id FROM work_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None or str(row["workspace_id"]) != workspace_id:
            raise PhaseDeliverableError(
                "phase_deliverable_not_found",
                "No phase deliverable history exists for this task and workspace.",
            )
        return list_phase_deliverable_index_page(
            self.store,
            work_item_id=item_id,
            workspace_id=workspace_id,
            cursor=cursor,
            page_size=page_size,
        )

    def _prepare_restart(self, item_id: str) -> dict[str, Any]:
        current = self.get(item_id, include_timeline=False)
        if current["status"] not in {"failed", "cancelled", "needs_attention"}:
            return current
        run_status = str((current.get("run") or {}).get("status") or "")
        if run_status == "awaiting_reconciliation":
            raise ValueError(
                "This item requires reconciliation before it can be started again."
            )
        revision = int(current["revision"]) + 1
        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE work_items
                SET revision=?, idempotency_key=?, status='draft', current_run_id=NULL,
                    error_code=NULL, error_reason=NULL, completed_at=NULL, updated_at=?
                WHERE id=?
                """,
                (revision, f"work-item:{item_id}:r{revision}", now, item_id),
            )
            self._event(
                connection,
                item_id,
                "work_item.retry_prepared",
                {"revision": revision, "previous_run_status": run_status},
            )
        return self.get(item_id, include_timeline=False)

    def mark_started(self, item_id: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            if str(row["status"]) in {"running", "cancelling"}:
                return self.get(item_id)
            connection.execute(
                """
                UPDATE work_items
                SET status='running', started_at=COALESCE(started_at, ?),
                    completed_at=NULL, archived_at=NULL, updated_at=?
                WHERE id=?
                """,
                (now, now, item_id),
            )
            self._event(connection, item_id, "work_item.started", {})
        return self.get(item_id)

    def record_result(self, item_id: str, result: dict[str, Any]) -> dict[str, Any]:
        run_id = result.get("run_id")
        run_status = result.get("status") or (result.get("final_report") or {}).get("status")
        status = _run_to_item_status(str(run_status or "failed"))
        if result.get("ok") is False and status == "draft":
            status = "failed"
        terminal = status in TERMINAL_ITEM_STATUSES
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            if run_id:
                self._link_run(connection, item_id, str(run_id))
            connection.execute(
                """
                UPDATE work_items
                SET current_run_id=COALESCE(?, current_run_id), status=?,
                    error_code=?, error_reason=?, updated_at=?,
                    completed_at=CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE completed_at END
                WHERE id=?
                """,
                (
                    run_id,
                    status,
                    error.get("code") or result.get("error_code"),
                    result.get("reason") or result.get("error_reason"),
                    now,
                    int(terminal),
                    now,
                    item_id,
                ),
            )
            self._event(
                connection,
                item_id,
                "work_item.result",
                {"run_id": run_id, "run_status": run_status, "status": status},
            )
        return self.get(item_id)

    def start(
        self,
        item_id: str,
        *,
        client_name: str = "generic-mcp",
        dry_run: bool = False,
        context7_libraries: list[str] | None = None,
    ) -> dict[str, Any]:
        item = self._prepare_restart(item_id)
        if item["status"] in {"running", "cancelling"}:
            raise ValueError("The work item is already running.")
        if item["status"] in {"completed", "archived"}:
            raise ValueError(f"A {item['status']} work item cannot be started.")
        # Validate workspace policy before materializing a running state. A
        # blocked run remains a durable draft that the client can configure
        # and retry without creating a phantom running item.
        protected_non_git = item.get("safety_mode") == "automatic"
        inspection = inspect_workspace(
            item["workspace_root"],
            access="write",
            protected_non_git=protected_non_git,
        )
        if item.get("safety_mode") == "non-git" and not inspection.get(
            "intentional_non_git"
        ):
            raise WorkspacePolicyError(
                "Non-Git mode requires explicit client/user consent.",
                code="workspace_non_git_confirmation_required",
                details=inspection,
            )
        require_workspace(
            item["workspace_root"],
            access="write",
            protected_non_git=protected_non_git,
        )
        if dry_run:
            from .workflows import run_workflow_impl

            execution = dict(item.get("config") or {})
            result = run_workflow_impl(
                workspace_root=str(item["workspace_root"]),
                task=str(item["task"]),
                extra_context=_context_with_attachments(item),
                architect_provider=execution.get("architect_provider"),
                implementer_provider=execution.get("implementer_provider"),
                reviewer_provider=execution.get("reviewer_provider"),
                max_rounds=execution.get("max_rounds"),
                context7_libraries=context7_libraries or execution.get("context7_libraries"),
                dry_run=True,
                client_name=client_name,
                workspace_mode=str(item["safety_mode"]),
                context7_policy=str(item["context_mode"]),
                role_profile_overrides=dict(item.get("role_profiles") or {}),
                # Dry-run preserves the historical facade/MCP semantics and
                # reports configured profiles without applying the workbench
                # convenience preset. Real starts apply the persisted preset.
                execution_preset=None,
                work_item_id=item_id,
            )
            result["work_item"] = self.get(item_id)
            return result

        self.mark_started(item_id)
        from .workflows import run_workflow_impl

        execution = dict(item.get("config") or {})
        result = run_workflow_impl(
            workspace_root=str(item["workspace_root"]),
            task=str(item["task"]),
            extra_context=_context_with_attachments(item),
            architect_provider=execution.get("architect_provider"),
            implementer_provider=execution.get("implementer_provider"),
            reviewer_provider=execution.get("reviewer_provider"),
            max_rounds=execution.get("max_rounds"),
            context7_libraries=context7_libraries or execution.get("context7_libraries"),
            dry_run=False,
            idempotency_key=str(item["idempotency_key"]),
            client_name=client_name,
            workspace_mode=str(item["safety_mode"]),
            context7_policy=str(item["context_mode"]),
            role_profile_overrides=dict(item.get("role_profiles") or {}),
            execution_preset=str(item["preset"]),
            work_item_id=item_id,
        )
        result["work_item"] = self.record_result(item_id, result)
        return result

    def cancel(
        self,
        item_id: str,
        *,
        reason: str = "Cancellation requested by client.",
        client_name: str = "generic-mcp",
    ) -> dict[str, Any]:
        item = self.get(item_id, include_timeline=False)
        run_id = item.get("current_run_id")
        if not run_id:
            raise ValueError("The work item has no durable run to cancel.")
        from .workflows import run_workflow_impl

        result = run_workflow_impl(
            workspace_root=str(item["workspace_root"]),
            task=str(item["task"]),
            resume_run_id=str(run_id),
            cancel=True,
            cancel_reason=reason,
            client_name=client_name,
            work_item_id=item_id,
        )
        result["work_item"] = self.record_result(item_id, result)
        return result

    def reconcile(
        self,
        item_id: str,
        *,
        action: str,
        client_name: str = "generic-mcp",
    ) -> dict[str, Any]:
        item = self.get(item_id, include_timeline=False)
        run_id = item.get("current_run_id")
        if not run_id:
            raise ValueError("The work item has no durable run to reconcile.")
        from .workflows import run_workflow_impl

        result = run_workflow_impl(
            workspace_root=str(item["workspace_root"]),
            task=str(item["task"]),
            resume_run_id=str(run_id),
            reconciliation_action=action,
            client_name=client_name,
            work_item_id=item_id,
        )
        result["work_item"] = self.record_result(item_id, result)
        return result

    def archive(self, item_id: str) -> dict[str, Any]:
        current = self.get(item_id, include_timeline=False)
        if current["status"] in {"running", "cancelling"}:
            raise ValueError("A running work item cannot be archived.")
        if current["status"] == "archived":
            return current
        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE work_items SET status='archived', archived_at=?, updated_at=? WHERE id=?",
                (now, now, item_id),
            )
            self._event(
                connection,
                item_id,
                "work_item.archived",
                {"previous_status": current["status"]},
            )
        return self.get(item_id)

    def restore(self, item_id: str) -> dict[str, Any]:
        current = self.get(item_id, include_timeline=False)
        if current["status"] != "archived":
            raise ValueError("Only an archived work item can be restored.")

        previous = self.store.connect().execute(
            """
            SELECT payload_json FROM work_item_events
            WHERE item_id=? AND event_type='work_item.archived'
            ORDER BY sequence DESC LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        archived_payload = _parse_json(
            str(previous["payload_json"]) if previous else None,
            {},
        )
        restored_status = str(archived_payload.get("previous_status") or "")
        if restored_status not in WORK_ITEM_STATUSES - {"archived"}:
            run = self.store.get_run(str(current["current_run_id"])) if current.get("current_run_id") else None
            restored_status = _run_to_item_status(str((run or {}).get("status") or ""))
            if restored_status in {"running", "cancelling"}:
                raise ValueError("A running work item cannot be restored from the archive.")
            if restored_status not in WORK_ITEM_STATUSES - {"archived"}:
                restored_status = "completed" if current.get("completed_at") else "draft"

        now = utc_now_iso()
        with self.store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE work_items SET status=?, archived_at=NULL, updated_at=? WHERE id=?",
                (restored_status, now, item_id),
            )
            self._event(
                connection,
                item_id,
                "work_item.restored",
                {"restored_status": restored_status},
            )
        return self.get(item_id)

    def delete(self, item_id: str) -> dict[str, Any]:
        """Permanently remove one archived item and its durable session data.

        Workspace files are never modified.  Provider sessions are deliberately
        retained because they may be shared by another active work item.
        """

        current = self.get(item_id, include_timeline=False)
        if current["status"] != "archived":
            raise ValueError("Only an archived work item can be deleted permanently.")

        connection = self.store.connect()
        run_ids = {
            str(row["run_id"])
            for row in connection.execute(
                "SELECT run_id FROM work_item_runs WHERE item_id=?",
                (item_id,),
            ).fetchall()
        }
        run_ids.update(
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM workflow_runs WHERE work_item_id=?",
                (item_id,),
            ).fetchall()
        )
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            shared_run_ids = {
                str(row["run_id"])
                for row in connection.execute(
                    f"SELECT DISTINCT run_id FROM work_item_runs WHERE item_id<>? AND run_id IN ({placeholders})",
                    (item_id, *sorted(run_ids)),
                ).fetchall()
            }
            # A run linked to another item is not this session's private
            # history. Keep it and its artifacts intact for that other item.
            run_ids.difference_update(shared_run_ids)
        private_artifact_ids = {
            str(value)
            for value in (
                current.get("task_artifact_id"),
                current.get("extra_context_artifact_id"),
            )
            if value
        }
        private_artifact_ids.update(
            str(row["artifact_id"])
            for row in connection.execute(
                """
                SELECT request_artifact_id AS artifact_id FROM work_item_turns WHERE item_id=?
                UNION
                SELECT context_artifact_id AS artifact_id FROM work_item_turns
                WHERE item_id=? AND context_artifact_id IS NOT NULL
                """,
                (item_id, item_id),
            ).fetchall()
            if row["artifact_id"]
        )

        clauses: list[str] = []
        params: list[str] = []
        if run_ids:
            clauses.append("run_id IN (" + ",".join("?" for _ in run_ids) + ")")
            params.extend(sorted(run_ids))
        if private_artifact_ids:
            clauses.append("id IN (" + ",".join("?" for _ in private_artifact_ids) + ")")
            params.extend(sorted(private_artifact_ids))

        removed_paths: list[str] = []
        with self.store.transaction(immediate=True) as transaction:
            if clauses:
                for row in transaction.execute(
                    "SELECT storage_path FROM artifacts WHERE " + " OR ".join(clauses),
                    tuple(params),
                ).fetchall():
                    if row["storage_path"]:
                        removed_paths.append(str(row["storage_path"]))
                transaction.execute(
                    "DELETE FROM artifacts WHERE " + " OR ".join(clauses),
                    tuple(params),
                )
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                transaction.execute(
                    f"DELETE FROM workflow_runs WHERE id IN ({placeholders})",
                    tuple(sorted(run_ids)),
                )
            transaction.execute("DELETE FROM work_items WHERE id=?", (item_id,))

        referenced_paths = {
            str(row["storage_path"])
            for row in self.store.connect().execute(
                "SELECT storage_path FROM artifacts WHERE storage_path IS NOT NULL"
            ).fetchall()
        }
        for raw_path in sorted(set(removed_paths) - referenced_paths):
            try:
                path = Path(raw_path)
                if path.exists():
                    path.unlink()
            except OSError:
                continue
        return {"id": item_id, "deleted": True}

    def summary(
        self,
        workspace_root: str | Path | None = None,
        *,
        limit: int = 100,
        selected_item_id: str | None = None,
        include_archived: bool = False,
        include_internal: bool = True,
    ) -> dict[str, Any]:
        resolved_root = None
        resolved_identity = None
        if workspace_root is not None:
            resolved_root, resolved_identity = _workspace(workspace_root)
        items = self.list(
            workspace_root=resolved_root,
            limit=limit,
            include_archived=include_archived,
            include_internal=include_internal,
            _workspace_id=(
                str(resolved_identity["workspace_id"])
                if resolved_identity is not None
                else None
            ),
        )
        if not include_internal:
            items = [compact_list_item(item) for item in items]
        counts: dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        selected = None
        selected_error = None
        if selected_item_id:
            selected_row = self.store.connect().execute(
                "SELECT workspace_id FROM work_items WHERE id=?", (selected_item_id,)
            ).fetchone()
            expected_workspace_id = (
                str(resolved_identity["workspace_id"])
                if resolved_identity is not None
                else None
            )
            if selected_row is None or (
                expected_workspace_id is not None
                and str(selected_row["workspace_id"]) != expected_workspace_id
            ):
                selected_error = "work_item_not_found"
            else:
                selected = self.get(
                    selected_item_id,
                    include_timeline=include_internal,
                    include_internal=include_internal,
                )
        return {
            "ok": True,
            "items": items,
            "selected": selected,
            "selected_error": selected_error,
            "counts": counts,
            "total": len(items),
            "preferences": (
                self._preferences_for_identity(resolved_root, resolved_identity)
                if resolved_root is not None and resolved_identity is not None
                else None
            )
            if include_internal
            else compact_preferences(
                self._preferences_for_identity(resolved_root, resolved_identity)
                if resolved_root is not None and resolved_identity is not None
                else None
            ),
            "profiles": available_execution_profiles()
            if include_internal
            else compact_execution_profiles(available_execution_profiles()),
            "options": workbench_options(),
        }


def available_execution_profiles() -> dict[str, Any]:
    cfg = load_config()
    return {
        "presets": ["fast", "balanced", "deep", "custom"],
        "execution_profiles": {
            name: asdict(profile) for name, profile in cfg.execution_profiles.items()
        },
        "roles": {
            role: {
                "profiles": list(cfg.roles[role].profiles),
                "strategy": cfg.roles[role].strategy,
                "resolution": cfg.roles[role].resolution,
            }
            for role in ROLE_NAMES
        },
        "resolved_roles": {
            role: [
                profile.to_dict()
                for profile in resolve_role_profiles(cfg, role, cfg.roles[role])
            ]
            for role in ROLE_NAMES
        },
    }


def upsert_execution_profile(
    name: str,
    *,
    provider: str,
    model: str = "",
    reasoning_effort: str = "",
    agent: str = "",
    effort: str = "",
    runner: str = "",
    session_scope: str = "",
    description: str = "",
) -> dict[str, Any]:
    clean = name.strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", clean):
        raise ValueError(
            "Profile name must be 1-64 letters, numbers, dots, underscores, or dashes."
        )
    cfg = load_config()
    cfg.execution_profiles[clean] = ExecutionProfileConfig(
        provider=provider.strip() or cfg.router.default_provider,
        model=model.strip(),
        reasoning_effort=reasoning_effort.strip(),
        agent=agent.strip(),
        effort=effort.strip(),
        runner=runner.strip(),
        session_scope=session_scope.strip(),
        enabled=True,
        description=description.strip(),
    )
    path = save_config(cfg)
    return {
        "ok": True,
        "profile": clean,
        "config": asdict(cfg.execution_profiles[clean]),
        "config_path": str(path),
    }
