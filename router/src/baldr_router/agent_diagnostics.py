from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .agent_api import (
    AgentContractError,
    AgentDigestMismatchError,
    AgentManifest,
)
from .agent_gateway import verify_kiro_agent_definition
from .agent_execution import local_process_health
from .durability.store import DurableStore
from .provider_registry import get_provider_registry


def _provider_health(name: str) -> dict[str, Any]:
    provider = str(name or "").strip()
    if not provider:
        return {
            "ok": False,
            "provider": "",
            "reason": "agent-provider-missing",
        }
    adapter = get_provider_registry().resolve(provider)
    if adapter is None:
        return {
            "ok": False,
            "provider": provider,
            "reason": "agent-provider-not-installed",
        }
    try:
        raw = adapter.status()
    except Exception:
        return {
            "ok": False,
            "provider": adapter.name,
            "reason": "agent-provider-status-failed",
        }
    status = raw if isinstance(raw, dict) else {}
    if "ok" in status:
        ok = status.get("ok") is True
    else:
        login = status.get("login")
        ok = bool(status.get("found")) and (
            not isinstance(login, dict) or login.get("ok") is True
        )
    result: dict[str, Any] = {"ok": ok, "provider": adapter.name}
    raw_version = status.get("version")
    if isinstance(raw_version, Mapping):
        version = str(raw_version.get("stdout") or "").strip().splitlines()[:1]
        if version:
            result["version"] = version[0][:128]
    elif raw_version:
        result["version"] = str(raw_version)[:128]
    if not ok:
        reason = str(status.get("reason") or "").strip()
        result["reason"] = reason or "agent-provider-unavailable"
        login = status.get("login")
        if isinstance(login, dict) and login.get("ok") is False:
            result["reason"] = str(
                login.get("reason") or "agent-provider-login-required"
            )
    return result


def _definition_health(
    manifest: AgentManifest, *, workspace_root: Path
) -> dict[str, Any]:
    provider = str(manifest.target.get("provider") or "").strip()
    if provider.lower().replace("_", "-") not in {"kiro", "kiro-cli"}:
        return {"ok": True, "attested": False}
    if manifest.target.get("definition_scope") == "builtin":
        try:
            verified = verify_kiro_agent_definition(
                target=manifest.target,
                cwd=workspace_root,
            )
        except AgentDigestMismatchError:
            return {
                "ok": False,
                "attested": True,
                "reason": "agent-definition-digest-mismatch",
            }
        except AgentContractError:
            return {
                "ok": False,
                "attested": True,
                "reason": "agent-definition-invalid",
            }
        return {
            "ok": True,
            "attested": True,
            "scope": "builtin",
            "digest": verified.get("source_fingerprint"),
        }
    if not manifest.target.get("definition_digest"):
        agent = str(manifest.target.get("agent") or "").strip()
        if (
            not agent
            or agent in {".", ".."}
            or "/" in agent
            or "\\" in agent
            or "\x00" in agent
        ):
            return {
                "ok": False,
                "attested": False,
                "reason": "agent-definition-invalid",
            }
        candidates = (
            workspace_root / ".kiro" / "agents" / f"{agent}.json",
            Path.home() / ".kiro" / "agents" / f"{agent}.json",
        )
        definition = next(
            (path for path in candidates if path.is_file() and not path.is_symlink()),
            None,
        )
        if definition is None:
            return {
                "ok": False,
                "attested": False,
                "reason": "agent-definition-missing",
            }
        return {
            "ok": True,
            "attested": False,
            "scope": "workspace" if definition == candidates[0] else "global",
        }
    try:
        verified = verify_kiro_agent_definition(
            target=manifest.target,
            cwd=workspace_root,
        )
    except AgentDigestMismatchError as exc:
        message = str(exc)
        if "unavailable" in message:
            reason = "agent-definition-missing"
        elif "shadows" in message:
            reason = "agent-definition-shadowed"
        else:
            reason = "agent-definition-digest-mismatch"
        return {"ok": False, "attested": True, "reason": reason}
    except AgentContractError:
        return {
            "ok": False,
            "attested": True,
            "reason": "agent-definition-invalid",
        }
    return {
        "ok": True,
        "attested": True,
        "scope": verified.get("definition_scope"),
        "digest": verified.get("definition_digest"),
    }


def diagnose_agent_manifest(
    manifest: AgentManifest,
    *,
    enabled: bool,
    workspace_root: Path,
    store: DurableStore | None = None,
) -> dict[str, Any]:
    """Project safe, actionable health and lifecycle metadata for one agent."""

    if not enabled:
        provider = {"ok": True, "provider": str(manifest.target.get("provider") or "")}
        definition = {
            "ok": True,
            "attested": bool(manifest.target.get("definition_digest")),
        }
        state = "disabled"
        ready = False
        reason = "agent-disabled"
    else:
        if manifest.transport == "provider":
            provider = _provider_health(str(manifest.target.get("provider") or ""))
            definition = _definition_health(manifest, workspace_root=workspace_root)
        elif manifest.transport == "local-process":
            provider = {
                **local_process_health(manifest),
                "provider": "external-local-process",
            }
            definition = {"ok": True, "attested": True}
        else:
            provider = {"ok": True, "provider": f"external-{manifest.transport}"}
            definition = {"ok": True, "attested": False}
        ready = bool(provider.get("ok") and definition.get("ok"))
        state = "ready" if ready else "unavailable"
        reason = (
            ""
            if ready
            else str(
                definition.get("reason")
                or provider.get("reason")
                or "agent-unavailable"
            )
        )

    lifecycle = {"last_execution": None, "last_success": None}
    if store is not None:
        try:
            lifecycle = store.agent_execution_status(str(manifest.reference))
        except Exception:
            lifecycle = {
                "last_execution": None,
                "last_success": None,
                "reason": "agent-lifecycle-unavailable",
            }
    return {
        "state": state,
        "ready": ready,
        "reason": reason or None,
        "provider_health": provider,
        "definition_health": definition,
        "last_execution": lifecycle.get("last_execution"),
        "last_success": lifecycle.get("last_success"),
    }
