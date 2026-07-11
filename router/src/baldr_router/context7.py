from __future__ import annotations

import hashlib
import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import load_config
from .redaction import redact_text, redact_value
from .secrets import read_context7_api_key
from .telemetry import app_cache_dir

CONTEXT7_BASE_URL = "https://context7.com"


def context7_cache_dir() -> Path:
    return app_cache_dir() / "context7"


def _cache_key(endpoint: str, params: dict[str, Any]) -> str:
    payload = json.dumps(
        {"endpoint": endpoint, "params": params}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(endpoint: str, params: dict[str, Any]) -> Path:
    return context7_cache_dir() / f"{_cache_key(endpoint, params)}.json"


def _read_cache(
    endpoint: str, params: dict[str, Any], ttl_hours: int
) -> dict[str, Any] | None:
    p = _cache_path(endpoint, params)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    fetched_at = float(data.get("fetched_at", 0))
    if ttl_hours > 0 and (time.time() - fetched_at) > ttl_hours * 3600:
        return None
    data["cache"] = {
        "hit": True,
        "path": str(p),
        "age_seconds": int(time.time() - fetched_at),
    }
    return data


def _write_cache(endpoint: str, params: dict[str, Any], status: int, body: Any) -> Path:
    p = _cache_path(endpoint, params)
    p.parent.mkdir(parents=True, exist_ok=True)
    persisted_params = {
        key: ("<query-redacted>" if key == "query" else redact_value(value))
        for key, value in params.items()
    }
    payload = {
        "fetched_at": time.time(),
        "endpoint": endpoint,
        "params": persisted_params,
        "status": status,
        "body": redact_value(body),
        "cache": {"hit": False, "path": str(p), "age_seconds": 0},
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def _http_get_json(
    endpoint: str, params: dict[str, Any], api_key: str, timeout: int = 30
) -> dict[str, Any]:
    url = f"{CONTEXT7_BASE_URL}{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"raw": raw}
            return {
                "ok": 200 <= res.status < 300,
                "status": res.status,
                "body": body,
                "url": url,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return {"ok": False, "status": exc.code, "body": body, "url": url}
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "body": {"error": type(exc).__name__, "message": redact_text(str(exc))},
            "url": url,
        }


def _cached_get(
    endpoint: str,
    params: dict[str, Any],
    api_key: str,
    ttl_hours: int,
    timeout: int = 30,
) -> dict[str, Any]:
    cached = _read_cache(endpoint, params, ttl_hours)
    if cached is not None:
        return {"ok": 200 <= int(cached.get("status", 0)) < 300, **cached}
    res = _http_get_json(endpoint, params, api_key, timeout=timeout)
    path = _write_cache(endpoint, params, int(res.get("status", 0)), res.get("body"))
    return {**res, "cache": {"hit": False, "path": str(path), "age_seconds": 0}}


def search_library(
    library_name: str, query: str = "", *, ttl_hours: int | None = None
) -> dict[str, Any]:
    cfg = load_config()
    api_key = read_context7_api_key(cfg.context7.api_key_source)
    if not api_key:
        return {
            "ok": False,
            "reason": "Context7 API key is not configured or not available.",
        }
    params: dict[str, Any] = {"libraryName": library_name}
    if query:
        params["query"] = redact_text(query)[:500]
    return _cached_get(
        "/api/v2/libs/search",
        params,
        api_key,
        ttl_hours or cfg.context7.cache_ttl_hours,
    )


def get_context(
    library_id: str,
    query: str,
    *,
    fast: bool | None = None,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    api_key = read_context7_api_key(cfg.context7.api_key_source)
    if not api_key:
        return {
            "ok": False,
            "reason": "Context7 API key is not configured or not available.",
        }
    params: dict[str, Any] = {
        "libraryId": library_id,
        "query": redact_text(query)[:500] or "implementation details",
        "type": "json",
    }
    if fast if fast is not None else cfg.context7.fast:
        params["fast"] = "true"
    return _cached_get(
        "/api/v2/context", params, api_key, ttl_hours or cfg.context7.cache_ttl_hours
    )


def _best_search_result(search_response: dict[str, Any]) -> dict[str, Any] | None:
    body = search_response.get("body") or {}
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or not results:
        return None
    # Context7 already ranks results. Prefer entries with an id.
    for item in results:
        if isinstance(item, dict) and item.get("id"):
            return item
    return results[0] if isinstance(results[0], dict) else None


def detect_workspace_libraries(
    workspace_root: str | Path, task_text: str = "", limit: int = 5
) -> list[str]:
    root = Path(workspace_root).expanduser().resolve()
    candidates: list[str] = []

    package_json = root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            for section in (
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            ):
                deps = data.get(section, {})
                if isinstance(deps, dict):
                    candidates.extend(str(k) for k in deps.keys())
        except Exception:
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project", {}) if isinstance(data, dict) else {}
            for dep in (
                project.get("dependencies", []) if isinstance(project, dict) else []
            ):
                name = (
                    str(dep)
                    .split("[", 1)[0]
                    .split("=", 1)[0]
                    .split("<", 1)[0]
                    .split(">", 1)[0]
                    .strip()
                )
                if name:
                    candidates.append(name)
        except Exception:
            pass

    go_mod = root / "go.mod"
    if go_mod.exists():
        try:
            for line in go_mod.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if (
                    not line
                    or line.startswith("module ")
                    or line.startswith("require (")
                    or line == ")"
                ):
                    continue
                if line.startswith("require "):
                    line = line.removeprefix("require ").strip()
                parts = line.split()
                if parts and "." in parts[0]:
                    candidates.append(parts[0])
        except Exception:
            pass

    # Score by mention in task text and by common frontend/backend framework relevance.
    text = task_text.lower()
    priority_terms = [
        "next",
        "react",
        "vue",
        "svelte",
        "astro",
        "nuxt",
        "nestjs",
        "express",
        "fastapi",
        "django",
        "flask",
        "drizzle",
        "prisma",
        "supabase",
        "tailwind",
        "tanstack",
        "redux",
        "zod",
        "trpc",
        "vite",
        "playwright",
        "vitest",
        "pytest",
    ]

    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    for raw in candidates:
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        lname = name.lower()
        score = 0
        normalized = lname.split("/")[-1]
        if normalized in text or lname in text:
            score += 100
        if any(term in lname for term in priority_terms):
            score += 25
        if score > 0:
            scored.append((score, name))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored[:limit]]


def lookup_docs_for_library(
    library: str, query: str, *, fast: bool | None = None
) -> dict[str, Any]:
    if library.startswith("/"):
        library_id = library
        search = {
            "ok": True,
            "body": {"results": [{"id": library_id, "title": library_id}]},
            "cache": {"hit": True},
        }
    else:
        search = search_library(library, query=query)
        if not search.get("ok"):
            return {
                "ok": False,
                "library": library,
                "stage": "search",
                "search": search,
            }
        best = _best_search_result(search)
        if not best or not best.get("id"):
            return {
                "ok": False,
                "library": library,
                "stage": "search",
                "reason": "No Context7 library match found.",
                "search": search,
            }
        library_id = str(best["id"])

    context = get_context(library_id, query, fast=fast)
    return {
        "ok": bool(context.get("ok")),
        "library": library,
        "library_id": library_id,
        "search": search,
        "context": context,
    }


def format_context_docs(result: dict[str, Any], max_chars: int = 5000) -> str:
    if not result.get("ok"):
        return ""
    body = (result.get("context") or {}).get("body") or {}
    parts: list[str] = []
    library_id = result.get("library_id") or result.get("library")
    parts.append(f"## Context7 docs for {library_id}")

    rules = body.get("rules") if isinstance(body, dict) else None
    if rules:
        parts.append("### Rules")
        parts.append(json.dumps(rules, ensure_ascii=False)[:1200])

    for info in (body.get("infoSnippets") or [])[:4] if isinstance(body, dict) else []:
        if not isinstance(info, dict):
            continue
        title = info.get("breadcrumb") or info.get("pageId") or "Info snippet"
        content = str(info.get("content") or "").strip()
        if content:
            parts.append(f"### {title}\n{content[:1200]}")

    for snippet in (
        (body.get("codeSnippets") or [])[:4] if isinstance(body, dict) else []
    ):
        if not isinstance(snippet, dict):
            continue
        title = snippet.get("codeTitle") or snippet.get("pageTitle") or "Code snippet"
        desc = snippet.get("codeDescription") or ""
        parts.append(f"### {title}\n{desc}".strip())
        for code_item in (snippet.get("codeList") or [])[:2]:
            if not isinstance(code_item, dict):
                continue
            lang = code_item.get("language") or snippet.get("codeLanguage") or "text"
            code = str(code_item.get("code") or "").strip()
            if code:
                parts.append(f"```{lang}\n{code[:1600]}\n```")

    text = "\n\n".join(parts).strip()
    return text[:max_chars]


def prepare_context7_bundle(
    *,
    workspace_root: str | Path,
    task_text: str,
    libraries: list[str] | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    if not cfg.context7.enabled:
        return {"enabled": False, "used": False, "reason": "Context7 is disabled."}
    if not cfg.context7.inject_docs:
        return {
            "enabled": True,
            "used": False,
            "reason": "Context7 doc injection is disabled.",
        }
    if cfg.context7.mode not in {"router-cache", "hybrid"}:
        return {
            "enabled": True,
            "used": False,
            "reason": f"Context7 mode is {cfg.context7.mode!r}; router-cache/hybrid is required for prefetch injection.",
        }

    api_key = read_context7_api_key(cfg.context7.api_key_source)
    if not api_key:
        return {
            "enabled": True,
            "used": False,
            "reason": "Context7 API key is missing.",
        }

    selected = libraries or detect_workspace_libraries(
        workspace_root, task_text, limit=cfg.context7.max_libraries
    )
    selected = selected[: cfg.context7.max_libraries]
    if not selected:
        return {
            "enabled": True,
            "used": False,
            "reason": "No candidate libraries were provided or detected.",
        }

    results: list[dict[str, Any]] = []
    docs_parts: list[str] = []
    remaining = cfg.context7.max_chars
    for lib in selected:
        if remaining <= 500:
            break
        res = lookup_docs_for_library(lib, task_text, fast=cfg.context7.fast)
        results.append(
            {
                "library": lib,
                "ok": res.get("ok"),
                "library_id": res.get("library_id"),
                "search_cache_hit": (
                    ((res.get("search") or {}).get("cache") or {}).get("hit")
                ),
                "context_cache_hit": (
                    ((res.get("context") or {}).get("cache") or {}).get("hit")
                ),
                "stage": res.get("stage"),
                "reason": res.get("reason"),
            }
        )
        text = format_context_docs(res, max_chars=remaining)
        if text:
            docs_parts.append(text)
            remaining -= len(text)

    bundle = "\n\n".join(docs_parts).strip()
    return {
        "enabled": True,
        "used": bool(bundle),
        "libraries": selected,
        "results": results,
        "bundle": bundle,
        "bundle_chars": len(bundle),
        "cache_dir": str(context7_cache_dir()),
    }


def cache_status() -> dict[str, Any]:
    d = context7_cache_dir()
    files = list(d.glob("*.json")) if d.exists() else []
    total_bytes = sum(p.stat().st_size for p in files if p.exists())
    return {"ok": True, "cache_dir": str(d), "files": len(files), "bytes": total_bytes}


def clear_cache(*, older_than_hours: int | None = None) -> dict[str, Any]:
    d = context7_cache_dir()
    if not d.exists():
        return {"ok": True, "cache_dir": str(d), "removed": 0}
    cutoff = None if older_than_hours is None else time.time() - older_than_hours * 3600
    removed = 0
    for p in d.glob("*.json"):
        try:
            if cutoff is not None and p.stat().st_mtime > cutoff:
                continue
            p.unlink()
            removed += 1
        except Exception:
            pass
    return {"ok": True, "cache_dir": str(d), "removed": removed}
