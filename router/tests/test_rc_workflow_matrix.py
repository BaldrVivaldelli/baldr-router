from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router import workflows


def _report(status: str, summary: str) -> dict:
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
    }


def test_ten_task_synthetic_release_candidate_matrix(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    repos = [tmp_path / "repo-a", tmp_path / "repo-b"]
    for repo in repos:
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git", "-C", str(repo), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"
            ],
            check=True,
        )
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(repo) for repo in repos])
    )

    def fake_provider(**kwargs):
        role = kwargs["role_name"]
        cwd: Path = kwargs["cwd"]
        if role == "architect":
            status = "planned"
        elif role == "implementer":
            status = "implemented"
            marker = cwd / "synthetic-provider-ran.txt"
            marker.write_text("implemented\n", encoding="utf-8")
        else:
            status = "approved"
        return {
            "ok": True,
            "provider": kwargs["provider"],
            "role": role,
            "final_report": _report(status, f"{role} completed"),
        }

    monkeypatch.setattr(workflows, "run_provider_role", fake_provider)

    tasks = [
        "add health endpoint",
        "refactor config loader",
        "add retry tests",
        "improve validation",
        "document cache behavior",
        "fix cancellation cleanup",
        "add status command",
        "harden workspace checks",
        "redact provider logs",
        "verify release packaging",
    ]
    results = []
    for index, task in enumerate(tasks):
        result = workflows.run_workflow_impl(
            workspace_root=str(repos[index % 2]),
            task=task,
            max_rounds=1,
        )
        results.append(result)
        if result.get("ok"):
            repo = repos[index % 2]
            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if status.strip():
                subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
                subprocess.run(
                    [
                        "git", "-C", str(repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm",
                        f"task-{index}",
                    ],
                    check=True,
                )

    assert len(results) == 10
    assert all(result["ok"] for result in results)
    assert all(result["status"] == "approved" for result in results)
    assert all(len(result["steps"]) == 3 for result in results)
    assert all((repo / "synthetic-provider-ran.txt").exists() for repo in repos)
