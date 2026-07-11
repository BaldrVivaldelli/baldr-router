from __future__ import annotations

import re
from typing import Any

from .codex_config import install_context7_mcp_config, remove_context7_mcp_config
from .config import Context7Config, load_config, save_config
from .context7 import cache_status
from .secrets import read_context7_api_key

VALID_CONTEXT7_MODES = {"off", "codex-mcp", "router-cache", "hybrid"}


def _safe_env_name(value: str) -> str:
    name = value.strip() or "CONTEXT7_API_KEY"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid environment variable name: {value!r}")
    return name


def context7_onboarding_plan() -> dict[str, Any]:
    """Return a non-secret Context7 onboarding plan for any MCP client.

    This deliberately does not ask for, return, or persist API keys. It gives
    the client a structured decision tree so it asks the user whether they
    want Context7 before showing setup commands.
    """
    cfg = load_config()
    key_available = bool(read_context7_api_key(cfg.context7.api_key_source))
    current = {
        "enabled": cfg.context7.enabled,
        "mode": cfg.context7.mode,
        "api_key_source": cfg.context7.api_key_source,
        "api_key_available": key_available,
        "install_codex_mcp": cfg.context7.install_codex_mcp,
        "inject_docs": cfg.context7.inject_docs,
        "cache": cache_status(),
    }

    return {
        "ok": True,
        "current": current,
        "ask_user_first": True,
        "recommended_question": (
            "¿Querés habilitar Context7 para enriquecer tareas con documentación actualizada? "
            "Elegí una opción: "
            "1) No por ahora. "
            "2) Sí, ya tengo una API key y quiero guardarla localmente. "
            "3) Sí, ya tengo CONTEXT7_API_KEY exportada en el entorno donde corre baldr-router. "
            "4) Todavía no tengo key; quiero instrucciones."
        ),
        "security_rule": "Do not ask the user to paste Context7 API keys into chat.",
        "choices": [
            {
                "id": "skip",
                "label": "No por ahora",
                "client_action": "Call context7_disable(remove_codex_mcp=false) or continue without Context7.",
            },
            {
                "id": "local_file",
                "label": "Sí, ya tengo una API key; guardarla localmente",
                "client_action": "Ask the user to run the setup command in a terminal with hidden input.",
                "command": "baldr-router setup-context7 --mode hybrid --install-codex-mcp",
                "note": "The command prompts for the key outside chat and stores it in the local secrets file with 0600 permissions.",
            },
            {
                "id": "env_var",
                "label": "Sí, ya tengo CONTEXT7_API_KEY exportada",
                "client_action": "Call context7_enable_env_source(mode='hybrid', env_name='CONTEXT7_API_KEY', install_codex_mcp=true).",
                "command": "baldr-router enable-context7-env --mode hybrid --install-codex-mcp",
                "note": "This stores only the env var name in config; the key must be available to the router process.",
            },
            {
                "id": "need_key",
                "label": "Todavía no tengo key",
                "client_action": "Explain where to create a Context7 key, then return to the local_file setup command.",
                "command_after_key": "baldr-router setup-context7 --mode hybrid --install-codex-mcp",
            },
        ],
        "default_if_user_is_unsure": "skip",
    }


def enable_context7_env_source(
    *,
    mode: str = "hybrid",
    env_name: str = "CONTEXT7_API_KEY",
    install_codex_mcp: bool = True,
    cache_ttl_hours: int = 48,
    inject_docs: bool = True,
    max_libraries: int = 3,
    max_chars: int = 9000,
    fast: bool = True,
    force_codex_mcp: bool = False,
) -> dict[str, Any]:
    """Enable Context7 using an existing environment variable, without secrets."""
    if mode not in VALID_CONTEXT7_MODES or mode == "off":
        return {"ok": False, "reason": f"Invalid enabled Context7 mode: {mode!r}."}

    env_name = _safe_env_name(env_name)
    source = f"env:{env_name}"
    cfg = load_config()
    cfg.context7 = Context7Config(
        enabled=True,
        mode=mode,
        api_key_source=source,
        install_codex_mcp=bool(install_codex_mcp or mode in {"codex-mcp", "hybrid"}),
        cache_ttl_hours=cache_ttl_hours,
        inject_docs=inject_docs,
        max_libraries=max_libraries,
        max_chars=max_chars,
        fast=fast,
    )
    saved = save_config(cfg)
    result: dict[str, Any] = {
        "ok": True,
        "config_path": str(saved),
        "context7": cfg.context7.__dict__,
        "api_key_available": bool(read_context7_api_key(source)),
        "next_step": None,
    }
    if cfg.context7.install_codex_mcp:
        result["codex_mcp"] = install_context7_mcp_config(force=force_codex_mcp)
    if not result["api_key_available"]:
        result["next_step"] = (
            f"Make sure {env_name} is exported in the environment used to start baldr-router."
        )
    return result


def disable_context7(*, remove_codex_mcp: bool = False) -> dict[str, Any]:
    cfg = load_config()
    cfg.context7.enabled = False
    cfg.context7.mode = "off"
    cfg.context7.install_codex_mcp = False
    saved = save_config(cfg)
    result: dict[str, Any] = {
        "ok": True,
        "config_path": str(saved),
        "context7_enabled": False,
        "context7_mode": "off",
    }
    if remove_codex_mcp:
        result["codex_mcp"] = remove_context7_mcp_config()
    return result
