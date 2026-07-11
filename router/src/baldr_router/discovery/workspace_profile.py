from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from baldr_router.config import load_config
from baldr_router.telemetry import app_cache_dir
from baldr_router.platforming import normalize_path_for_runtime
from baldr_router.workspace_policy import inspect_workspace

from .exclusions import (
    LANGUAGE_EXTENSIONS,
    excluded_directory,
    is_manifest,
    is_sensitive_file,
)
from .fingerprint import file_sha256, path_id, stable_json_hash

SCHEMA_VERSION = 1

_FRAMEWORK_PACKAGES = {
    "next": "Next.js",
    "react": "React",
    "vue": "Vue",
    "@angular/core": "Angular",
    "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit",
    "nuxt": "Nuxt",
    "express": "Express",
    "fastify": "Fastify",
    "nestjs": "NestJS",
    "@nestjs/core": "NestJS",
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "starlette": "Starlette",
    "sqlalchemy": "SQLAlchemy",
    "pytest": "pytest",
    "ruff": "Ruff",
    "pydantic": "Pydantic",
    "tokio": "Tokio",
    "axum": "Axum",
    "actix-web": "Actix Web",
    "spring-boot": "Spring Boot",
}

_SCRIPT_GROUPS = {
    "test": ("test", "spec", "pytest"),
    "lint": ("lint", "ruff", "eslint"),
    "typecheck": ("typecheck", "type-check", "mypy", "pyright", "tsc"),
    "build": ("build", "compile", "package"),
    "format": ("format", "fmt", "prettier"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_root() -> Path:
    return app_cache_dir() / "workspaces"


def _cache_path(root: Path) -> Path:
    return _cache_root() / path_id(root) / f"profile-v{SCHEMA_VERSION}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _run_git(root: Path, *args: str, timeout: int = 10) -> tuple[int, str]:
    git = shutil.which("git")
    if not git:
        return 127, ""
    try:
        completed = subprocess.run(
            [git, "-C", str(root), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return 1, ""
    return completed.returncode, completed.stdout.strip()


def _git_metadata(root: Path) -> dict[str, Any]:
    branch_code, branch = _run_git(root, "branch", "--show-current")
    head_code, head = _run_git(root, "rev-parse", "--short=12", "HEAD")
    status_code, status = _run_git(root, "status", "--porcelain=v1", "-uno")
    count_code, count = _run_git(root, "ls-files")
    tracked_count = len(count.splitlines()) if count_code == 0 else None
    return {
        "available": bool(shutil.which("git")),
        "branch": branch if branch_code == 0 and branch else None,
        "head": head if head_code == 0 and head else None,
        "dirty": bool(status) if status_code == 0 else None,
        "tracked_files": tracked_count,
    }


def _git_files(root: Path, max_files: int) -> list[Path] | None:
    code, output = _run_git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        timeout=30,
    )
    if code != 0:
        return None
    entries = [entry for entry in output.split("\x00") if entry]
    files: list[Path] = []
    for entry in entries[:max_files]:
        path = root / entry
        if path.is_symlink():
            continue
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file() and not is_sensitive_file(path):
            files.append(path)
    return files


def _walk_files(root: Path, *, max_files: int, max_depth: int) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            continue
        dirs[:] = [
            name
            for name in dirs
            if not excluded_directory(name) and depth < max_depth
        ]
        for name in names:
            path = current_path / name
            if path.is_symlink() or is_sensitive_file(path):
                continue
            try:
                path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            files.append(path)
            if len(files) >= max_files:
                return files
    return files


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _read_text(path: Path, max_bytes: int) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_package_json(path: Path, max_dependencies: int) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "node", "parse_error": True}
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    dependencies: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = data.get(key)
        if isinstance(values, dict):
            dependencies.extend(str(name) for name in values)
    dependencies = sorted(set(dependencies))[:max_dependencies]
    package_manager = str(data.get("packageManager") or "").split("@", 1)[0] or None
    return {
        "kind": "node",
        "name": data.get("name"),
        "private": data.get("private"),
        "package_manager": package_manager,
        "scripts": sorted(str(k) for k in scripts),
        "dependencies": dependencies,
        "workspaces": data.get("workspaces"),
    }


def _parse_pyproject(path: Path, max_dependencies: int) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "python", "parse_error": True}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    poetry = (
        data.get("tool", {}).get("poetry", {})
        if isinstance(data.get("tool"), dict)
        and isinstance(data.get("tool", {}).get("poetry"), dict)
        else {}
    )
    dependencies: list[str] = []
    raw_dependencies = project.get("dependencies")
    if isinstance(raw_dependencies, list):
        for item in raw_dependencies:
            name = re.split(r"[<>=!~;\[]", str(item), maxsplit=1)[0].strip()
            if name:
                dependencies.append(name)
    poetry_dependencies = poetry.get("dependencies")
    if isinstance(poetry_dependencies, dict):
        dependencies.extend(str(name) for name in poetry_dependencies if name != "python")
    scripts = project.get("scripts") if isinstance(project.get("scripts"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    return {
        "kind": "python",
        "name": project.get("name") or poetry.get("name"),
        "requires_python": project.get("requires-python"),
        "scripts": sorted(str(k) for k in scripts),
        "dependencies": sorted(set(dependencies))[:max_dependencies],
        "tools": sorted(str(name) for name in tool.keys()),
        "build_backend": (
            data.get("build-system", {}).get("build-backend")
            if isinstance(data.get("build-system"), dict)
            else None
        ),
    }


def _parse_cargo(path: Path, max_dependencies: int) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "rust", "parse_error": True}
    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    deps: list[str] = []
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            deps.extend(str(name) for name in value)
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    return {
        "kind": "rust",
        "name": package.get("name"),
        "dependencies": sorted(set(deps))[:max_dependencies],
        "workspace_members": workspace.get("members"),
    }


def _parse_go_mod(path: Path) -> dict[str, Any]:
    text = _read_text(path, 1024 * 1024)
    module = None
    dependencies: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            module = stripped.split(None, 1)[1]
        elif stripped and not stripped.startswith("//") and re.match(r"^[A-Za-z0-9_.-]+\.[^\s]+\s+v", stripped):
            dependencies.append(stripped.split()[0])
    return {
        "kind": "go",
        "name": module,
        "dependencies": sorted(set(dependencies))[:200],
    }


def _parse_makefile(path: Path, max_bytes: int) -> dict[str, Any]:
    text = _read_text(path, max_bytes)
    targets: list[str] = []
    for line in text.splitlines():
        if line.startswith((" ", "\t", ".")) or ":=" in line or "=" in line.split(":", 1)[0]:
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?![=])", line)
        if match:
            targets.append(match.group(1))
    return {"kind": "make", "targets": sorted(set(targets))[:100]}


def _parse_manifest(path: Path, max_bytes: int, max_dependencies: int) -> dict[str, Any]:
    name = path.name
    if name == "package.json":
        return _parse_package_json(path, max_dependencies)
    if name == "pyproject.toml":
        return _parse_pyproject(path, max_dependencies)
    if name == "Cargo.toml":
        return _parse_cargo(path, max_dependencies)
    if name == "go.mod":
        return _parse_go_mod(path)
    if name == "Makefile":
        return _parse_makefile(path, max_bytes)
    if name.endswith((".sln", ".csproj", ".fsproj", ".vbproj")):
        return {"kind": "dotnet", "name": path.stem}
    if name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
        return {"kind": "jvm", "name": path.parent.name}
    if name in {"Gemfile"}:
        return {"kind": "ruby"}
    if name == "composer.json":
        return {"kind": "php"}
    if name == "mix.exs":
        return {"kind": "elixir"}
    if name == "pubspec.yaml":
        return {"kind": "dart"}
    return {"kind": "metadata"}


def _package_manager(root: Path, manifests: list[Path], parsed: list[dict[str, Any]]) -> list[str]:
    names = {path.name for path in manifests}
    managers: set[str] = set()
    for item in parsed:
        manager = item.get("package_manager")
        if manager:
            managers.add(str(manager))
    if "pnpm-lock.yaml" in names or "pnpm-workspace.yaml" in names:
        managers.add("pnpm")
    if "yarn.lock" in names:
        managers.add("yarn")
    if "package-lock.json" in names:
        managers.add("npm")
    if "bun.lock" in names or "bun.lockb" in names:
        managers.add("bun")
    if "uv.lock" in names:
        managers.add("uv")
    if "poetry.lock" in names:
        managers.add("poetry")
    if "pdm.lock" in names:
        managers.add("pdm")
    if "Cargo.lock" in names:
        managers.add("cargo")
    if "go.mod" in names:
        managers.add("go")
    if any(path.name.endswith((".sln", ".csproj", ".fsproj", ".vbproj")) for path in manifests):
        managers.add("dotnet")
    return sorted(managers)


def _recommended_commands(parsed: list[dict[str, Any]], managers: list[str]) -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {key: [] for key in _SCRIPT_GROUPS}
    node_prefix = "npm run"
    if "pnpm" in managers:
        node_prefix = "pnpm"
    elif "yarn" in managers:
        node_prefix = "yarn"
    elif "bun" in managers:
        node_prefix = "bun run"

    tools: set[str] = set()
    for manifest in parsed:
        scripts = manifest.get("scripts")
        if isinstance(scripts, (dict, list)):
            for script in scripts:
                lower = str(script).lower()
                for group, needles in _SCRIPT_GROUPS.items():
                    if any(needle in lower for needle in needles):
                        commands[group].append(f"{node_prefix} {script}")
        raw_tools = manifest.get("tools")
        if isinstance(raw_tools, list):
            tools.update(str(item) for item in raw_tools)
        if manifest.get("kind") == "make":
            for target in manifest.get("targets") or []:
                lower = str(target).lower()
                for group, needles in _SCRIPT_GROUPS.items():
                    if any(needle in lower for needle in needles):
                        commands[group].append(f"make {target}")

    python_prefix = "uv run " if "uv" in managers else ""
    if "pytest" in tools:
        commands["test"].append(f"{python_prefix}pytest")
    if "ruff" in tools:
        commands["lint"].append(f"{python_prefix}ruff check .")
        commands["format"].append(f"{python_prefix}ruff format --check .")
    if "mypy" in tools:
        commands["typecheck"].append(f"{python_prefix}mypy .")
    if "pyright" in tools:
        commands["typecheck"].append(f"{python_prefix}pyright")
    if "cargo" in managers:
        commands["test"].append("cargo test")
        commands["lint"].append("cargo clippy --all-targets --all-features")
        commands["format"].append("cargo fmt --check")
        commands["build"].append("cargo build")
    if "go" in managers:
        commands["test"].append("go test ./...")
        commands["build"].append("go build ./...")
    if "dotnet" in managers:
        commands["test"].append("dotnet test")
        commands["build"].append("dotnet build")

    return {key: list(dict.fromkeys(values))[:20] for key, values in commands.items() if values}


def _frameworks(parsed: Iterable[dict[str, Any]]) -> list[str]:
    dependencies: set[str] = set()
    for item in parsed:
        values = item.get("dependencies")
        if isinstance(values, list):
            dependencies.update(str(value).lower() for value in values)
    detected = {
        display
        for package, display in _FRAMEWORK_PACKAGES.items()
        if package.lower() in dependencies
    }
    return sorted(detected)


def _manifest_fingerprint(root: Path, manifests: list[Path], git: dict[str, Any], max_bytes: int) -> str:
    items: list[dict[str, Any]] = []
    for path in manifests:
        try:
            stat = path.stat()
            digest = file_sha256(path, max_bytes=max_bytes)
        except OSError:
            continue
        items.append(
            {
                "path": _relative(root, path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest,
            }
        )
    return stable_json_hash({"manifests": items, "git_head": git.get("head")})


def workspace_profile_status(workspace_root: str | Path) -> dict[str, Any]:
    root = normalize_path_for_runtime(workspace_root)
    cache = _cache_path(root)
    value = _read_json(cache) if cache.exists() else None
    return {
        "ok": bool(value and value.get("ok")),
        "available": value is not None,
        "cache_path": str(cache),
        "generated_at": value.get("generated_at") if value else None,
        "fingerprint": value.get("fingerprint") if value else None,
        "workspace_id": path_id(root),
    }


def workspace_profile(
    workspace_root: str | Path,
    *,
    refresh: bool = False,
    require_trusted: bool = True,
) -> dict[str, Any]:
    cfg = load_config()
    policy = inspect_workspace(workspace_root, access="read")
    if require_trusted and not policy.get("ok"):
        return {
            "ok": False,
            "skipped": True,
            "schema_version": SCHEMA_VERSION,
            "reason": "Workspace profiling requires a trusted workspace.",
            "workspace_policy": policy,
            "privacy": {"source_files_read": False, "manifests_read": False},
        }
    root = Path(str(policy.get("path") or workspace_root)).resolve()
    cache_path = _cache_path(root)
    cached = _read_json(cache_path) if cache_path.exists() else None
    ttl_seconds = max(0, int(cfg.probe.cache_ttl_minutes)) * 60
    if cached and not refresh:
        generated_epoch = float(cached.get("generated_epoch") or 0)
        if generated_epoch and time.time() - generated_epoch <= ttl_seconds:
            cached = dict(cached)
            cached["cache"] = {
                "hit": True,
                "path": str(cache_path),
                "ttl_minutes": cfg.probe.cache_ttl_minutes,
            }
            return cached

    files = _git_files(root, cfg.probe.max_files)
    source = "git-ls-files"
    if files is None:
        files = _walk_files(
            root,
            max_files=cfg.probe.max_files,
            max_depth=cfg.probe.scan_max_depth,
        )
        source = "bounded-filesystem-walk"

    manifests = sorted((path for path in files if is_manifest(path)), key=lambda p: _relative(root, p))
    parsed: list[dict[str, Any]] = []
    manifest_records: list[dict[str, Any]] = []
    dependency_names: set[str] = set()
    for path in manifests:
        item = _parse_manifest(
            path,
            cfg.probe.max_manifest_bytes,
            cfg.probe.max_dependency_names,
        )
        item["path"] = _relative(root, path)
        parsed.append(item)
        values = item.get("dependencies")
        if isinstance(values, list):
            dependency_names.update(str(value) for value in values)
        manifest_records.append(
            {
                "path": _relative(root, path),
                "kind": item.get("kind"),
                "parse_error": bool(item.get("parse_error")),
            }
        )

    language_counts: Counter[str] = Counter()
    total_bytes = 0
    for path in files:
        language = LANGUAGE_EXTENSIONS.get(path.suffix.lower())
        if language:
            language_counts[language] += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass

    git = _git_metadata(root)
    managers = _package_manager(root, manifests, parsed)
    fingerprint = _manifest_fingerprint(root, manifests, git, cfg.probe.max_manifest_bytes)
    now = time.time()
    profile = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "generated_epoch": now,
        "fingerprint": fingerprint,
        "workspace": {
            "id": path_id(root),
            "name": root.name,
            "root": str(root),
            "trusted": bool(policy.get("trusted")),
            "trusted_by": policy.get("trusted_by"),
            "git_root": policy.get("git_root"),
        },
        "git": git,
        "inventory": {
            "source": source,
            "files_considered": len(files),
            "max_files": cfg.probe.max_files,
            "approx_bytes": total_bytes,
            "languages": dict(language_counts.most_common()),
            "manifests": manifest_records,
        },
        "ecosystem": {
            "package_managers": managers,
            "frameworks": _frameworks(parsed),
            "dependencies": sorted(dependency_names)[: cfg.probe.max_dependency_names],
            "manifest_details": parsed,
        },
        "recommended_commands": _recommended_commands(parsed, managers),
        "cache": {
            "hit": False,
            "path": str(cache_path),
            "ttl_minutes": cfg.probe.cache_ttl_minutes,
        },
        "privacy": {
            "deep_source_content_read": False,
            "manifest_content_read": True,
            "sensitive_file_patterns_excluded": True,
            "gitignore_respected": source == "git-ls-files",
            "scripts_executed": False,
        },
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return profile
