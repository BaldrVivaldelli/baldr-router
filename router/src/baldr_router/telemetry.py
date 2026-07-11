from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .redaction import redact_value

APP_NAME = "baldr-router"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def xdg_state_home() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "state"


def xdg_cache_home() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".cache"


def app_state_dir() -> Path:
    return xdg_state_home() / APP_NAME


def app_cache_dir() -> Path:
    return xdg_cache_home() / APP_NAME


def runs_jsonl_path() -> Path:
    return app_state_dir() / "runs.jsonl"


def append_run(record: dict[str, Any]) -> Path:
    p = runs_jsonl_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = redact_value(dict(record))
    normalized.setdefault("recorded_at", utc_now_iso())
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n")
    return p


def iter_runs(path: Path | None = None) -> Iterable[dict[str, Any]]:
    p = path or runs_jsonl_path()
    if not p.exists():
        return []

    def _gen() -> Iterable[dict[str, Any]]:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    return _gen()


def recent_runs(limit: int = 20) -> dict[str, Any]:
    runs = list(iter_runs())
    return {
        "ok": True,
        "path": str(runs_jsonl_path()),
        "count": len(runs),
        "runs": runs[-limit:],
    }


def telemetry_stats() -> dict[str, Any]:
    runs = list(iter_runs())
    durations = [
        int(r.get("duration_ms", 0))
        for r in runs
        if isinstance(r.get("duration_ms"), int)
    ]
    providers = Counter(str(r.get("provider", "unknown")) for r in runs)
    runners = Counter(str(r.get("runner", "unknown")) for r in runs)
    ok_count = sum(1 for r in runs if r.get("ok") is True)
    usage_totals: Counter[str] = Counter()
    for r in runs:
        usage = r.get("usage") or {}
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, int):
                    usage_totals[key] += value
    return {
        "ok": True,
        "path": str(runs_jsonl_path()),
        "count": len(runs),
        "ok_count": ok_count,
        "error_count": len(runs) - ok_count,
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else 0,
        "max_duration_ms": max(durations) if durations else 0,
        "providers": dict(providers),
        "runners": dict(runners),
        "usage_totals": dict(usage_totals),
    }
