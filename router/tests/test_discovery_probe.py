from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router.discovery.environment_probe import environment_probe
from baldr_router.discovery.workspace_profile import workspace_profile, workspace_profile_status


def test_environment_probe_is_redacted_and_fingerprinted(monkeypatch):
    monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7sk-synthetic-secret-probe-value")
    result = environment_probe(client_id="test-client")
    encoded = json.dumps(result)
    assert result["ok"] is True
    assert len(result["fingerprint"]) == 64
    assert result["client"]["id"] == "test-client"
    assert "ctx7sk-synthetic-secret-probe-value" not in encoded


def test_workspace_profile_requires_trust(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = workspace_profile(repo)
    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["privacy"]["source_files_read"] is False


def test_workspace_profile_discovers_manifests_and_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "probe-fixture",
                "packageManager": "pnpm@9.0.0",
                "scripts": {"test": "vitest", "build": "tsc"},
                "dependencies": {"react": "latest", "next": "latest"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    monkeypatch.setenv("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(repo)]))

    first = workspace_profile(repo)
    second = workspace_profile(repo)
    status = workspace_profile_status(repo)

    assert first["ok"] is True
    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert "pnpm" in first["ecosystem"]["package_managers"]
    assert set(first["ecosystem"]["frameworks"]) >= {"Next.js", "React"}
    assert first["recommended_commands"]["test"] == ["pnpm test"]
    assert first["inventory"]["languages"]["TypeScript"] == 1
    assert first["privacy"]["deep_source_content_read"] is False
    assert status["available"] is True
