from __future__ import annotations

import re
import tempfile
import tomllib
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from baldr_agent_sdk.contract import ContractError

from .models import ProjectSpec, RoleSpec


PROJECT_FILE = "baldr-agent.toml"
PROJECT_SCHEMA_VERSION = 2
LEGACY_PROJECT_SCHEMA_VERSION = 1

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_LANGUAGE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_DRIVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_MOVING_VERSIONS = {"latest", "current", "stable"}


def validate_name(value: Any, field: str) -> str:
    result = _text(value, field, limit=96).lower()
    if not _NAME.fullmatch(result):
        raise ContractError(f"{field} is not a valid lowercase identifier.")
    return result


def validate_exact_version(value: Any) -> str:
    result = _text(value, "version", limit=64)
    if not _VERSION.fullmatch(result) or result.lower() in _MOVING_VERSIONS:
        raise ContractError("version must be exact and immutable.")
    return result


def _text(value: Any, field: str, *, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string.")
    result = value.strip()
    if len(result) > limit:
        raise ContractError(f"{field} exceeds {limit} characters.")
    return result


def _relative_path(value: Any, field: str) -> PurePosixPath:
    text = _text(value, field, limit=512).replace("\\", "/")
    result = PurePosixPath(text)
    if result.is_absolute() or ".." in result.parts or not result.parts:
        raise ContractError(f"{field} must remain inside the agent project.")
    return result


def _string_list(value: Any, field: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ContractError(f"{field} must be a non-empty bounded array.")
    result = tuple(_text(item, field, limit=512) for item in value)
    if len(set(result)) != len(result):
        raise ContractError(f"{field} must not contain duplicates.")
    return result


def _regular_project_file(root: Path, relative: PurePosixPath) -> bool:
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return False
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return candidate.is_file()


def load_project(path: str | Path = ".") -> ProjectSpec:
    selected = Path(path).expanduser()
    config_path = selected if selected.is_file() else selected / PROJECT_FILE
    config_path = config_path.resolve()
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"{PROJECT_FILE} was not found at {config_path}.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"{PROJECT_FILE} is invalid TOML: {exc}.") from exc
    if not isinstance(document, dict):
        raise ContractError(f"{PROJECT_FILE} must contain a TOML object.")
    common_allowed = {
        "schema_version",
        "name",
        "owner",
        "registry",
        "namespace",
        "version",
        "sources",
        "output_dir",
        "timeout_seconds",
        "test_command",
        "source_id",
        "roles",
    }
    schema_version = document.get("schema_version")
    if schema_version == LEGACY_PROJECT_SCHEMA_VERSION:
        allowed = common_allowed | {"entry_module"}
    elif schema_version == PROJECT_SCHEMA_VERSION:
        allowed = common_allowed | {"language", "entrypoint", "driver"}
    else:
        raise ContractError(
            f"{PROJECT_FILE} requires schema_version = 1 or {PROJECT_SCHEMA_VERSION}."
        )
    unexpected = sorted(set(document) - allowed)
    if unexpected:
        raise ContractError(
            f"Unexpected {PROJECT_FILE} fields: {', '.join(unexpected)}."
        )
    version = validate_exact_version(document.get("version"))
    if schema_version == LEGACY_PROJECT_SCHEMA_VERSION:
        language = "python"
        driver = None
        entry_module = _text(document.get("entry_module"), "entry_module", limit=240)
        if not _MODULE.fullmatch(entry_module):
            raise ContractError("entry_module must be an importable Python module name.")
        entrypoint = PurePosixPath(entry_module.replace(".", "/") + ".py")
        if not _regular_project_file(config_path.parent, entrypoint):
            package_entrypoint = PurePosixPath(
                entry_module.replace(".", "/") + "/__init__.py"
            )
            if _regular_project_file(config_path.parent, package_entrypoint):
                entrypoint = package_entrypoint
    else:
        language = _text(document.get("language"), "language", limit=32).lower()
        if not _LANGUAGE.fullmatch(language):
            raise ContractError("language is not a valid language identifier.")
        entrypoint = _relative_path(document.get("entrypoint"), "entrypoint")
        raw_driver = document.get("driver")
        driver = (
            _text(raw_driver, "driver", limit=160) if raw_driver is not None else None
        )
        if driver is not None and not _DRIVER.fullmatch(driver):
            raise ContractError("driver is not a valid driver identifier.")
        expected_suffix = {"python": ".py", "typescript": ".ts"}.get(language)
        if expected_suffix and entrypoint.suffix != expected_suffix:
            raise ContractError(
                f"{language} entrypoint must end with {expected_suffix!r}."
            )
    timeout = document.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 86400:
        raise ContractError("timeout_seconds must be between 1 and 86400.")
    source_id = _text(document.get("source_id"), "source_id", limit=96).lower()
    if not _SOURCE_ID.fullmatch(source_id):
        raise ContractError("source_id is invalid.")
    raw_roles = document.get("roles")
    if not isinstance(raw_roles, dict) or not raw_roles or len(raw_roles) > 16:
        raise ContractError("roles must contain between 1 and 16 role definitions.")
    roles: list[RoleSpec] = []
    for raw_key, raw_value in raw_roles.items():
        key = str(raw_key).strip().lower()
        if not _ROLE.fullmatch(key) or not isinstance(raw_value, dict):
            raise ContractError(f"Invalid role definition: {raw_key!r}.")
        unexpected_role = sorted(
            set(raw_value)
            - {"agent_name", "capabilities", "effect_mode", "label", "description"}
        )
        if unexpected_role:
            raise ContractError(
                f"Unexpected role {key!r} fields: {', '.join(unexpected_role)}."
            )
        effect_mode = _text(raw_value.get("effect_mode"), f"roles.{key}.effect_mode")
        if effect_mode not in {"read-only", "workspace-write", "external"}:
            raise ContractError(f"roles.{key}.effect_mode is invalid.")
        capabilities = _string_list(
            raw_value.get("capabilities"), f"roles.{key}.capabilities"
        )
        if effect_mode == "workspace-write" and "workspace.write" not in capabilities:
            raise ContractError(f"Writable role {key!r} must declare workspace.write.")
        roles.append(
            RoleSpec(
                key=key,
                agent_name=validate_name(
                    raw_value.get("agent_name"), f"roles.{key}.agent_name"
                ),
                capabilities=capabilities,
                effect_mode=effect_mode,
                label=_text(raw_value.get("label"), f"roles.{key}.label"),
                description=_text(
                    raw_value.get("description"),
                    f"roles.{key}.description",
                    limit=512,
                ),
            )
        )
    if len({role.agent_name for role in roles}) != len(roles):
        raise ContractError("Every role must publish a distinct agent_name.")
    project = ProjectSpec(
        root=config_path.parent,
        schema_version=int(schema_version),
        name=validate_name(document.get("name"), "name"),
        owner=_text(document.get("owner"), "owner"),
        registry=validate_name(document.get("registry"), "registry"),
        namespace=validate_name(document.get("namespace"), "namespace"),
        version=version,
        language=language,
        entrypoint=entrypoint,
        driver=driver,
        sources=tuple(
            _relative_path(item, "sources")
            for item in _string_list(document.get("sources"), "sources")
        ),
        output_dir=_relative_path(document.get("output_dir", "dist"), "output_dir"),
        timeout_seconds=timeout,
        test_command=_string_list(document.get("test_command"), "test_command"),
        source_id=source_id,
        roles=tuple(roles),
    )
    for role in project.roles:
        project.reference(role)
    if not _regular_project_file(project.root, project.entrypoint):
        raise ContractError("entrypoint must be a regular source file.")
    if not any(
        project.entrypoint == source
        or project.entrypoint.parts[: len(source.parts)] == source.parts
        for source in project.sources
    ):
        raise ContractError("entrypoint must be included by sources.")
    return project


def set_project_version(path: str | Path, version: str) -> dict[str, Any]:
    """Atomically set the exact release version without rewriting project TOML."""

    project = load_project(path)
    selected = validate_exact_version(version)
    config_path = project.root / PROJECT_FILE
    original = config_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^(version[ \t]*=[ \t]*)(?P<quote>['\"])[^'\"]*(?P=quote)"
        r"([ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(original))
    if len(matches) != 1:
        raise ContractError(
            f"{PROJECT_FILE} must contain one quoted version assignment."
        )
    updated = pattern.sub(
        lambda match: (
            f"{match.group(1)}{match.group('quote')}{selected}"
            f"{match.group('quote')}{match.group(3)}"
        ),
        original,
        count=1,
    )
    if updated == original:
        return {
            "ok": True,
            "project": str(project.root),
            "config": str(config_path),
            "previous_version": project.version,
            "version": selected,
            "changed": False,
        }

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{PROJECT_FILE}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(updated)
            temporary_path = Path(handle.name)
        validated = load_project(temporary_path)
        if validated.version != selected:
            raise ContractError("The updated project version could not be validated.")
        temporary_path.chmod(config_path.stat().st_mode & 0o7777)
        temporary_path.replace(config_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()

    return {
        "ok": True,
        "project": str(project.root),
        "config": str(config_path),
        "previous_version": project.version,
        "version": selected,
        "changed": True,
    }
