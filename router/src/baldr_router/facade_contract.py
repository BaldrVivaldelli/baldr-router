from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

CONTRACT_NAME = "baldr-facade"
CONTRACT_VERSION = "1.0.0"
INTENT_ORDER = ("setup", "status", "run")


@dataclass(frozen=True, slots=True)
class FacadeIntent:
    id: str
    title: str
    description: str
    requires_workspace: bool
    requires_task: bool
    mcp_prompt: str
    cli: tuple[str, ...]
    aliases: dict[str, str]
    instruction: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "requiresWorkspace": self.requires_workspace,
            "requiresTask": self.requires_task,
            "mcpPrompt": self.mcp_prompt,
            "cli": list(self.cli),
            "aliases": dict(self.aliases),
            "instruction": self.instruction,
        }


def _contract_text() -> str:
    return (
        resources.files("baldr_router.contracts")
        .joinpath("facade-v1.json")
        .read_text(encoding="utf-8")
    )


def _validate_contract(data: dict[str, Any]) -> None:
    if data.get("contract") != CONTRACT_NAME:
        raise ValueError(f"Unsupported facade contract: {data.get('contract')!r}")
    if data.get("version") != CONTRACT_VERSION:
        raise ValueError(f"Unsupported facade version: {data.get('version')!r}")
    if data.get("product") != "baldr-router":
        raise ValueError("Facade contract product must be 'baldr-router'.")

    command_palette = data.get("commandPalette")
    if not isinstance(command_palette, dict):
        raise ValueError("commandPalette must be an object.")
    if command_palette.get("primary") != "Baldr: Open":
        raise ValueError("The only primary command must be 'Baldr: Open'.")
    if command_palette.get("maximumVisibleCommands") != 1:
        raise ValueError("The facade contract allows exactly one visible command.")

    intents = data.get("intents")
    if not isinstance(intents, dict):
        raise ValueError("intents must be an object.")
    if tuple(intents) != INTENT_ORDER:
        raise ValueError(
            f"Intent order must be {INTENT_ORDER!r}; got {tuple(intents)!r}."
        )

    required = {
        "title",
        "description",
        "requiresWorkspace",
        "requiresTask",
        "mcpPrompt",
        "cli",
        "aliases",
        "instruction",
    }
    for intent_id, raw in intents.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Intent {intent_id!r} must be an object.")
        missing = required - set(raw)
        if missing:
            raise ValueError(
                f"Intent {intent_id!r} is missing fields: {sorted(missing)}"
            )
        if raw["mcpPrompt"] != intent_id:
            raise ValueError(f"Intent {intent_id!r} must use the same MCP prompt name.")
        if raw["cli"] != ["facade", intent_id]:
            raise ValueError(
                f"Intent {intent_id!r} must map to `baldr-router facade {intent_id}`."
            )
        aliases = raw.get("aliases")
        if not isinstance(aliases, dict):
            raise ValueError(f"Intent {intent_id!r} aliases must be an object.")


@lru_cache(maxsize=1)
def facade_contract() -> dict[str, Any]:
    data = json.loads(_contract_text())
    if not isinstance(data, dict):
        raise ValueError("Facade contract root must be an object.")
    _validate_contract(data)
    return data


@lru_cache(maxsize=1)
def facade_intents() -> tuple[FacadeIntent, ...]:
    contract = facade_contract()
    return tuple(
        FacadeIntent(
            id=intent_id,
            title=str(raw["title"]),
            description=str(raw["description"]),
            requires_workspace=bool(raw["requiresWorkspace"]),
            requires_task=bool(raw["requiresTask"]),
            mcp_prompt=str(raw["mcpPrompt"]),
            cli=tuple(str(part) for part in raw["cli"]),
            aliases={str(key): str(value) for key, value in raw["aliases"].items()},
            instruction=str(raw["instruction"]),
        )
        for intent_id, raw in contract["intents"].items()
    )


def get_facade_intent(intent_id: str) -> FacadeIntent:
    normalized = intent_id.strip().lower().lstrip("/")
    for intent in facade_intents():
        if intent.id == normalized:
            return intent
    raise ValueError(
        f"Unknown Baldr facade intent {intent_id!r}. "
        f"Available: {', '.join(INTENT_ORDER)}."
    )


def facade_contract_status() -> dict[str, Any]:
    contract = facade_contract()
    return {
        "contract": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "product": "baldr-router",
        "command_palette": dict(contract["commandPalette"]),
        "intents": [intent.as_dict() for intent in facade_intents()],
        "facade_rules": list(contract["facadeRules"]),
    }


def render_facade_prompt(
    intent_id: str,
    *,
    workspace_root: str | None = None,
    task: str | None = None,
) -> str:
    intent = get_facade_intent(intent_id)
    lines = [f"Baldr Router intent: {intent.title}", "", intent.instruction]

    if workspace_root:
        lines.extend(["", f"Workspace root: {workspace_root}"])
    elif intent.requires_workspace:
        lines.extend(
            [
                "",
                "Use the current trusted workspace root. Ask only when no workspace is open.",
            ]
        )

    if task and task.strip():
        lines.extend(["", "Task:", task.strip()])
    elif intent.requires_task:
        lines.extend(
            [
                "",
                "Use the task supplied after the command. Ask for one concise task if it is empty.",
            ]
        )

    return "\n".join(lines).strip()
