from __future__ import annotations

import os
from importlib import metadata
from typing import Any

EXTENSION_ENTRY_POINT_GROUP = "baldr_router.extensions"

_loaded = False
_results: list[dict[str, Any]] = []


def _entry_points() -> list[Any]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=EXTENSION_ENTRY_POINT_GROUP))
    return list(discovered.get(EXTENSION_ENTRY_POINT_GROUP, []))  # type: ignore[attr-defined]


def load_installed_extensions(mcp: Any) -> list[dict[str, Any]]:
    """Load installed client adapters into the running MCP server once.

    Extensions are local Python packages explicitly installed into the same
    environment as ``baldr-router``. The core does not import client-specific
    modules or assume any particular client.
    """

    global _loaded, _results
    if _loaded:
        return list(_results)
    _loaded = True

    if os.environ.get("BALDR_ROUTER_DISABLE_EXTENSIONS") == "1":
        _results = [{"name": None, "loaded": False, "disabled": True}]
        return list(_results)

    results: list[dict[str, Any]] = []
    for entry_point in sorted(_entry_points(), key=lambda item: item.name):
        record: dict[str, Any] = {
            "name": entry_point.name,
            "value": entry_point.value,
            "group": entry_point.group,
            "loaded": False,
        }
        try:
            register = entry_point.load()
            metadata_result = register(mcp)
            record["loaded"] = True
            if isinstance(metadata_result, dict):
                record.update(metadata_result)
        except Exception as exc:  # pragma: no cover - environment/plugin boundary
            record["error"] = f"{type(exc).__name__}: {exc}"
        results.append(record)

    _results = results
    return list(_results)


def extension_status() -> dict[str, Any]:
    discovered = [
        {"name": ep.name, "value": ep.value, "group": ep.group}
        for ep in sorted(_entry_points(), key=lambda item: item.name)
    ]
    return {
        "ok": not any(item.get("error") for item in _results),
        "loaded": _loaded,
        "entry_point_group": EXTENSION_ENTRY_POINT_GROUP,
        "discovered": discovered,
        "results": list(_results),
        "disabled_by_env": os.environ.get("BALDR_ROUTER_DISABLE_EXTENSIONS") == "1",
    }


def _reset_for_tests() -> None:
    global _loaded, _results
    _loaded = False
    _results = []
