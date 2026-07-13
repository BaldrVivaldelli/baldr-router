from __future__ import annotations

from pathlib import Path

from baldr_router.facade import facade_status_report


def _isolated_runtime(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_workbench_only_status_skips_expensive_health_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    _isolated_runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def unexpected_doctor(*_args, **_kwargs):
        raise AssertionError("workbench-only status must not run doctor")

    monkeypatch.setattr("baldr_router.facade_runtime.doctor", unexpected_doctor)

    result = facade_status_report(
        str(workspace),
        client="vscode-extension",
        workbench_only=True,
    )

    assert result["ok"] is True
    assert result["view"] == "workbench"
    assert result["client"] == "vscode-extension"
    assert result["summary"] == {"work_item_counts": {}}
    assert result["workbench"]["items"] == []
    assert result["workbench"]["selected"] is None
    assert "health" not in result
    assert "qualification" not in result
    assert "recent_runs" not in result
