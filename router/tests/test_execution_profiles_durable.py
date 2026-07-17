from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router import workflows
from baldr_router.config import ExecutionProfileConfig, load_config, save_config
from baldr_router.execution_profiles import role_execution_plan


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


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
        "decisions": {"write_authorization": "not_required"},
    }


def test_one_shared_profile_or_n_m_l_profiles_per_phase(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = load_config()
    assert cfg.roles["architect"].profiles == ["default"]
    assert cfg.roles["implementer"].profiles == ["default"]
    assert cfg.roles["reviewer"].profiles == ["default"]

    cfg.execution_profiles.update(
        {
            "arch-a": ExecutionProfileConfig(provider="codex", model="architecture-a", reasoning_effort="high"),
            "arch-b": ExecutionProfileConfig(provider="kiro-cli", agent="architecture-b", effort="medium"),
            "impl-a": ExecutionProfileConfig(provider="codex", model="implementation-a", reasoning_effort="medium"),
            "review-a": ExecutionProfileConfig(provider="codex", model="review-a", reasoning_effort="high"),
            "review-b": ExecutionProfileConfig(provider="codex", model="review-b", reasoning_effort="low"),
            "review-c": ExecutionProfileConfig(provider="kiro-cli", agent="review-c", effort="high"),
        }
    )
    cfg.roles["architect"].profiles = ["arch-a", "arch-b"]
    cfg.roles["implementer"].profiles = ["impl-a"]
    cfg.roles["reviewer"].profiles = ["review-a", "review-b", "review-c"]
    save_config(cfg)

    loaded = load_config()
    assert len(role_execution_plan(loaded, "architect", loaded.roles["architect"])["profiles"]) == 2
    assert len(role_execution_plan(loaded, "implementer", loaded.roles["implementer"])["profiles"]) == 1
    assert len(role_execution_plan(loaded, "reviewer", loaded.roles["reviewer"])["profiles"]) == 3


def test_role_model_effort_and_sessions_are_dispatched_from_profiles(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(repo)]))

    cfg = load_config()
    cfg.execution_profiles = {
        "architecture": ExecutionProfileConfig(
            provider="codex", model="architecture-model", reasoning_effort="high", session_scope="workspace"
        ),
        "implementation": ExecutionProfileConfig(
            provider="codex", model="implementation-model", reasoning_effort="medium", session_scope="workspace"
        ),
        "review": ExecutionProfileConfig(
            provider="codex", model="review-model", reasoning_effort="high", session_scope="workspace"
        ),
    }
    cfg.roles["architect"].profiles = ["architecture"]
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = ["review"]
    save_config(cfg)

    captured: list[dict] = []

    def fake_provider(**kwargs):
        captured.append(kwargs)
        role = kwargs["role_name"]
        if role == "implementer":
            (kwargs["cwd"] / "implemented.txt").write_text("ok\n", encoding="utf-8")
        status = {"architect": "planned", "implementer": "implemented", "reviewer": "approved"}[role]
        return {
            "ok": True,
            "run_id": f"provider-{len(captured)}",
            "thread_id": f"thread-{kwargs['profile_name']}",
            "final_report": _report(status, role),
        }

    monkeypatch.setattr(workflows, "run_provider_role", fake_provider)
    result = workflows.run_workflow_impl(workspace_root=str(repo), task="Implement fixture")

    assert result["ok"] is True
    by_role = {item["role_name"]: item for item in captured}
    assert by_role["architect"]["model"] == "architecture-model"
    assert by_role["architect"]["reasoning_effort"] == "high"
    assert by_role["implementer"]["model"] == "implementation-model"
    assert by_role["implementer"]["reasoning_effort"] == "medium"
    assert by_role["reviewer"]["model"] == "review-model"
    assert (repo / "implemented.txt").read_text(encoding="utf-8") == "ok\n"
    assert result["durable"]["session_count"] == 3


def test_phase_strategies_support_fallback_and_multi_participant_review(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo-strategies"
    _init_repo(repo)
    monkeypatch.setenv("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(repo)]))

    cfg = load_config()
    cfg.execution_profiles = {
        "arch-failing": ExecutionProfileConfig(provider="codex", model="arch-failing"),
        "arch-good": ExecutionProfileConfig(provider="codex", model="arch-good"),
        "implementation": ExecutionProfileConfig(provider="codex", model="implementation"),
        "review-a": ExecutionProfileConfig(provider="codex", model="review-a"),
        "review-b": ExecutionProfileConfig(provider="codex", model="review-b"),
    }
    cfg.roles["architect"].profiles = ["arch-failing", "arch-good"]
    cfg.roles["architect"].strategy = "first-success"
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = ["review-a", "review-b"]
    cfg.roles["reviewer"].strategy = "all"
    cfg.roles["reviewer"].min_successes = 2
    cfg.workflows["architect-implement-review"].max_rounds = 0
    save_config(cfg)

    calls: list[tuple[str, str]] = []

    def fake_provider(**kwargs):
        calls.append((kwargs["role_name"], kwargs["profile_name"]))
        if kwargs["profile_name"] == "arch-failing":
            return {"ok": False, "reason": "synthetic fallback"}
        role = kwargs["role_name"]
        status = {"architect": "planned", "implementer": "implemented", "reviewer": "approved"}[role]
        return {
            "ok": True,
            "run_id": f"provider-{len(calls)}",
            "final_report": _report(status, kwargs["profile_name"]),
        }

    monkeypatch.setattr(workflows, "run_provider_role", fake_provider)
    result = workflows.run_workflow_impl(
        workspace_root=str(repo),
        task="Exercise n/m/l phase strategies",
        max_rounds=0,
    )
    assert result["ok"] is True
    assert calls == [
        ("architect", "arch-failing"),
        ("architect", "arch-good"),
        ("implementer", "implementation"),
        ("reviewer", "review-a"),
        ("reviewer", "review-b"),
    ]
    review_step = next(step for step in result["steps"] if step["phase"] == "reviewer")
    assert len(review_step["profiles"]) == 2
    assert all(profile["status"] == "succeeded" for profile in review_step["profiles"])


def test_multiple_write_profiles_cannot_use_all_strategy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = load_config()
    cfg.execution_profiles = {
        "write-a": ExecutionProfileConfig(provider="codex"),
        "write-b": ExecutionProfileConfig(provider="codex"),
    }
    role = cfg.roles["implementer"]
    role.profiles = ["write-a", "write-b"]
    role.strategy = "all"
    try:
        role_execution_plan(cfg, "implementer", role)
    except ValueError as exc:
        assert "cannot use strategy='all'" in str(exc)
    else:
        raise AssertionError("write-enabled multi-profile 'all' strategy must be rejected")
