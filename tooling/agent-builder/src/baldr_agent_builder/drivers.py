from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import baldr_agent_sdk
from baldr_agent_sdk.contract import ContractError

from .protocol import (
    DRIVER_CONTRACT,
    PROTOCOL_VERSION,
    validate_driver_message,
)


REGISTRATION_CONTRACT = "baldr-builder-driver-registration"
REGISTRATION_VERSION = 1
DRIVER_PATHS_ENV = "BALDR_BUILDER_DRIVER_PATHS"


def _default_driver_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "baldr_agent_builder.driver")


@dataclass(frozen=True)
class DriverProcess:
    command: Sequence[str] = _default_driver_command()
    origin: str = "configured"

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        timeout_seconds: int = 1800,
    ) -> Mapping[str, Any]:
        env = os.environ.copy()
        roots = [
            str(Path(__file__).resolve().parents[1]),
            str(Path(baldr_agent_sdk.__file__).resolve().parents[1]),
        ]
        previous = env.get("PYTHONPATH")
        if previous:
            roots.append(previous)
        env["PYTHONPATH"] = os.pathsep.join(roots)
        try:
            process = subprocess.run(
                list(self.command),
                input=json.dumps(request, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                text=True,
                capture_output=True,
                check=False,
                env=env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContractError(
                f"Builder driver exceeded its {timeout_seconds}s deadline."
            ) from exc
        if process.returncode:
            detail = (process.stderr or process.stdout).strip()
            raise ContractError(
                f"Builder driver exited with code {process.returncode}: {detail}"
            )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise ContractError("Builder driver returned an invalid JSONL response.")
        try:
            response = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise ContractError("Builder driver returned invalid JSON.") from exc
        message = validate_driver_message(
            response,
            kinds={"describe-response", "operation-result"},
        )
        if message.get("request_id") != request.get("request_id"):
            raise ContractError("Builder driver response request_id does not match.")
        return message


@dataclass(frozen=True)
class DiscoveredDriver:
    descriptor: Mapping[str, Any]
    process: DriverProcess

    def identity(self) -> dict[str, Any]:
        return {
            "id": self.descriptor["id"],
            "version": self.descriptor["version"],
            "digest": self.descriptor["digest"],
        }


@dataclass(frozen=True)
class DriverDiscovery:
    drivers: tuple[DiscoveredDriver, ...]
    errors: tuple[Mapping[str, str], ...]


def _config_root() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return (base / "baldr-agent" / "builder-drivers").resolve()


def _resolve_registration(path: Path) -> DriverProcess:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Driver registration is invalid: {path}.") from exc
    if not isinstance(value, dict):
        raise ContractError("Driver registration must be a JSON object.")
    if (
        value.get("contract") != REGISTRATION_CONTRACT
        or value.get("version") != REGISTRATION_VERSION
    ):
        raise ContractError("Driver registration contract is unsupported.")
    unexpected = sorted(set(value) - {"contract", "version", "command"})
    command = value.get("command")
    if unexpected or not isinstance(command, list) or not command or len(command) > 64:
        raise ContractError("Driver registration fields are invalid.")
    resolved: list[str] = []
    for index, item in enumerate(command):
        if not isinstance(item, str) or not item.strip() or len(item) > 4096:
            raise ContractError("Driver registration command is invalid.")
        text = item.strip()
        candidate = path.parent / text
        if text.startswith(("./", "../")):
            text = str(candidate.resolve())
        elif index == 0 and ("/" in text or "\\" in text):
            text = str(Path(text).expanduser().resolve())
        resolved.append(text)
    executable = shutil.which(resolved[0])
    if executable:
        resolved[0] = executable
    elif not Path(resolved[0]).is_file():
        raise ContractError(f"Driver command was not found: {resolved[0]}.")
    return DriverProcess(tuple(resolved), origin=str(path))


def _registration_paths() -> list[Path]:
    paths: list[Path] = []
    configured = os.environ.get(DRIVER_PATHS_ENV, "")
    for item in configured.split(os.pathsep):
        if item.strip():
            paths.append(Path(item).expanduser().resolve())
    root = _config_root()
    if root.is_dir() and not root.is_symlink():
        paths.extend(sorted(root.glob("*.json")))
    return paths


def _path_processes() -> list[DriverProcess]:
    processes: list[DriverProcess] = []
    seen: set[Path] = set()
    suffixes = (".exe", ".cmd", ".bat") if os.name == "nt" else ("",)
    for raw_root in os.environ.get("PATH", "").split(os.pathsep):
        root = Path(raw_root)
        if not root.is_dir():
            continue
        for suffix in suffixes:
            for candidate in sorted(root.glob("baldr-builder-driver-*" + suffix)):
                resolved = candidate.resolve()
                if resolved in seen or not candidate.is_file():
                    continue
                seen.add(resolved)
                processes.append(DriverProcess((str(resolved),), origin="PATH"))
                if len(processes) >= 32:
                    return processes
    return processes


class DriverRegistry:
    def __init__(self, processes: Sequence[DriverProcess] | None = None) -> None:
        self._explicit = tuple(processes) if processes is not None else None

    def _candidates(self) -> tuple[list[DriverProcess], list[Mapping[str, str]]]:
        if self._explicit is not None:
            return list(self._explicit), []
        candidates = [DriverProcess(_default_driver_command(), origin="built-in")]
        errors: list[Mapping[str, str]] = []
        for path in _registration_paths():
            try:
                candidates.append(_resolve_registration(path))
            except ContractError as exc:
                errors.append({"origin": str(path), "message": str(exc)})
        candidates.extend(_path_processes())
        return candidates, errors

    def discover(self) -> DriverDiscovery:
        candidates, errors = self._candidates()
        records: list[DiscoveredDriver] = []
        seen_commands: set[tuple[str, ...]] = set()
        seen_identities: set[tuple[str, str, str]] = set()
        for index, process in enumerate(candidates):
            command = tuple(process.command)
            if command in seen_commands:
                continue
            seen_commands.add(command)
            request_id = f"discover-{index + 1}"
            try:
                response = process.invoke(
                    {
                        "contract": DRIVER_CONTRACT,
                        "version": PROTOCOL_VERSION,
                        "kind": "describe-request",
                        "request_id": request_id,
                    },
                    timeout_seconds=30,
                )
                descriptor = response["driver"]
                identity = (
                    str(descriptor["id"]),
                    str(descriptor["version"]),
                    str(descriptor["digest"]),
                )
                if identity in seen_identities:
                    continue
                seen_identities.add(identity)
                records.append(DiscoveredDriver(dict(descriptor), process))
            except (ContractError, OSError, ValueError) as exc:
                errors.append({"origin": process.origin, "message": str(exc)})
        records.sort(
            key=lambda item: (
                str(item.descriptor["language"]),
                str(item.descriptor["id"]),
                str(item.descriptor["version"]),
                str(item.descriptor["digest"]),
            )
        )
        return DriverDiscovery(tuple(records), tuple(errors))

    def resolve(
        self,
        identity: Mapping[str, Any],
        *,
        language: str,
        operation: str,
        target: str,
    ) -> DiscoveredDriver:
        matches = []
        for record in self.discover().drivers:
            descriptor = record.descriptor
            if (
                descriptor.get("id") == identity.get("id")
                and descriptor.get("version") == identity.get("version")
                and descriptor.get("digest") == identity.get("digest")
                and descriptor.get("language") == language
                and operation in descriptor.get("operations", [])
                and target in descriptor.get("targets", [])
            ):
                matches.append(record)
        if len(matches) != 1:
            raise ContractError("The exact compatible Builder driver is not available.")
        return matches[0]


def register_driver(path: str | Path) -> Mapping[str, Any]:
    registration = Path(path).expanduser().resolve()
    process = _resolve_registration(registration)
    response = process.invoke(
        {
            "contract": DRIVER_CONTRACT,
            "version": PROTOCOL_VERSION,
            "kind": "describe-request",
            "request_id": "register-driver",
        },
        timeout_seconds=30,
    )
    descriptor = dict(response["driver"])
    safe_id = str(descriptor["id"]).replace(":", "-")
    destination = _config_root() / f"{safe_id}-{descriptor['version']}.json"
    value = {
        "contract": REGISTRATION_CONTRACT,
        "version": REGISTRATION_VERSION,
        "command": list(process.command),
    }
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file():
            raise ContractError("Existing driver registration is unsafe.")
        if destination.read_text(encoding="utf-8") != encoded:
            raise ContractError(
                "This driver id and version already use a different command."
            )
        reused = True
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        reused = False
    return {
        "ok": True,
        "registration": str(destination),
        "driver": descriptor,
        "reused": reused,
    }


def driver_status(driver_id: str | None = None) -> Mapping[str, Any]:
    discovery = DriverRegistry().discover()
    drivers = [
        {**dict(item.descriptor), "origin": item.process.origin}
        for item in discovery.drivers
        if driver_id is None or item.descriptor.get("id") == driver_id
    ]
    return {
        "ok": bool(drivers) if driver_id is not None else True,
        "drivers": drivers,
        "errors": [dict(item) for item in discovery.errors],
    }
