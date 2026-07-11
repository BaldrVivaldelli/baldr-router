from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def _load_resource(name: str) -> dict[str, Any]:
    raw = files("baldr_router.qualification").joinpath(name).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"Qualification resource {name} must contain an object.")
    return value


def qualification_profiles() -> dict[str, Any]:
    return _load_resource("profiles-v1.json")


def canary_definition() -> dict[str, Any]:
    return _load_resource("canaries-v1.json")


def qualification_profile(profile_id: str) -> dict[str, Any]:
    definitions = qualification_profiles()
    profiles = definitions.get("profiles") or {}
    try:
        profile = dict(profiles[profile_id])
    except KeyError as exc:
        available = ", ".join(sorted(profiles))
        raise ValueError(f"Unknown qualification profile {profile_id!r}. Available: {available}") from exc
    profile["id"] = profile_id
    profile["common_assertions"] = list(definitions.get("common_assertions") or [])
    profile["acceptance"] = dict(definitions.get("acceptance") or {})
    profile["all_required_assertions"] = [
        *profile["common_assertions"],
        *list(profile.get("required_assertions") or []),
    ]
    return profile
