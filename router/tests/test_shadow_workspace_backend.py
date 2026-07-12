from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from baldr_router.durability import shadow_workspace as shadow_workspace_module
from baldr_router.durability.shadow_workspace import (
    ShadowConflictError,
    ShadowPolicy,
    ShadowPolicyError,
    ShadowStateError,
    ShadowWorkspaceManager,
    scan_workspace,
)


def _manager(tmp_path: Path, **policy: object) -> ShadowWorkspaceManager:
    return ShadowWorkspaceManager(
        state_root=tmp_path / "state",
        policy=ShadowPolicy.from_dict(policy),
    )


def _require_symlink_capability(tmp_path: Path) -> None:
    """Skip only after proving this process cannot create a file symlink."""

    target = tmp_path / ".baldr-symlink-probe-target"
    link = tmp_path / ".baldr-symlink-probe-link"
    target.write_text("probe", encoding="utf-8")
    try:
        os.symlink(target.name, link)
        if not link.is_symlink():
            pytest.skip("filesystem did not create a symbolic link")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links unavailable for this process: {exc}")
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def _require_directory_symlink_capability(tmp_path: Path) -> None:
    target = tmp_path / ".baldr-directory-symlink-probe-target"
    link = tmp_path / ".baldr-directory-symlink-probe-link"
    target.mkdir()
    try:
        os.symlink(target.name, link, target_is_directory=True)
        if not link.is_symlink():
            pytest.skip("filesystem did not create a directory symbolic link")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links unavailable for this process: {exc}")
    finally:
        if os.path.lexists(link):
            try:
                link.unlink()
            except IsADirectoryError:
                link.rmdir()
        target.rmdir()


def _managed_snapshot(root: Path) -> dict[str, tuple[str, bytes | str | int]]:
    result: dict[str, tuple[str, bytes | str | int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory", stat.S_IMODE(path.stat().st_mode))
    return result


def test_prepare_creates_durable_content_addressed_copy_and_private_git(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "readme.txt").write_text("hello", encoding="utf-8")
    (source / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (source / ".env.example").write_text("TOKEN=example", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "large.js").write_text("generated", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("private metadata", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / ".git").mkdir()
    (source / "nested" / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (source / "nested" / "tracked.txt").write_text("nested content", encoding="utf-8")

    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="run-one", workspace_root=source)

    assert execution.mode == "shadow"
    assert execution.execution_root == tmp_path / "state" / "shadow-workspaces" / "run-one" / "tree"
    assert (execution.control_root / "state.json").is_file()
    assert (execution.control_root / "journal.json").is_file()
    assert (execution.execution_root / "readme.txt").read_text() == "hello"
    assert (execution.execution_root / ".env.example").is_file()
    assert not (execution.execution_root / ".env").exists()
    assert not (execution.execution_root / "node_modules").exists()
    assert not (execution.execution_root / "nested" / ".git").exists()
    assert (execution.execution_root / "nested" / "tracked.txt").is_file()

    state = json.loads((execution.control_root / "state.json").read_text())
    digest = state["base_manifest"]
    manifest_path = execution.control_root / "manifests" / f"{digest}.json"
    assert manifest_path.is_file()
    assert execution.base_manifest == digest == execution.checkpoint_manifest
    blob_files = [item for item in (execution.control_root / "blobs").rglob("*") if item.is_file()]
    assert blob_files
    assert all(len(item.name) == 64 for item in blob_files)
    assert state["source_scan"]["exclusions"] == {
        "generated": 1,
        "sensitive": 1,
        "vcs_metadata": 2,
    }
    # Git is private execution state and deliberately excluded from manifests.
    assert state["private_git"]["available"] is True
    assert state["private_git"]["commit"]
    assert (execution.execution_root / ".git").is_dir()
    assert manager.inspect(execution)["tree_matches_checkpoint"] is True


def test_prepare_fails_when_private_git_exists_but_has_no_usable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "readme.txt").write_text("hello", encoding="utf-8")

    def failed_commit(
        workspace: object, *, message: str = "BALDR baseline"
    ) -> dict[str, object]:
        del message
        tree = workspace.tree  # type: ignore[attr-defined]
        (tree / ".git").mkdir()
        return {
            "available": False,
            "commit": None,
            "reason": "simulated-commit-failure",
        }

    monkeypatch.setattr(
        shadow_workspace_module._ShadowWorkspace,
        "_git_init",
        failed_commit,
    )

    with pytest.raises(ShadowPolicyError) as raised:
        _manager(tmp_path).prepare(run_id="unusable-private-git", workspace_root=source)

    assert raised.value.code == "shadow_private_git_unavailable"
    assert raised.value.details == {"reason": "simulated-commit-failure"}


def test_manifest_is_deterministic_and_policy_is_content_addressed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "b.txt").write_text("b")
    (source / "a.txt").write_text("a")
    policy = ShadowPolicy()
    first = scan_workspace(source, policy).manifest
    second = scan_workspace(source, policy).manifest
    assert first.canonical_bytes == second.canonical_bytes
    assert first.digest == second.digest

    changed_policy = ShadowPolicy(max_files=policy.max_files - 1)
    assert scan_workspace(source, changed_policy).manifest.digest != first.digest


@pytest.mark.parametrize(
    ("policy", "contents", "limit"),
    [
        ({"max_files": 1}, {"a": b"a", "b": b"b"}, "max_files"),
        ({"max_file_bytes": 1}, {"a": b"ab"}, "max_file_bytes"),
        ({"max_total_bytes": 1}, {"a": b"a", "b": b"b"}, "max_total_bytes"),
    ],
)
def test_prepare_hard_fails_visible_limits(
    tmp_path: Path,
    policy: dict[str, int],
    contents: dict[str, bytes],
    limit: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name, value in contents.items():
        (source / name).write_bytes(value)
    manager = _manager(tmp_path, **policy)
    with pytest.raises(ShadowPolicyError) as raised:
        manager.prepare(run_id=f"limit-{limit}", workspace_root=source)
    assert raised.value.code == "shadow_limit_exceeded"
    assert raised.value.details["limit"] == limit


def test_max_files_limit_counts_directories_and_stops_directory_bombs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "directory-bomb"
    source.mkdir()
    for index in range(4):
        (source / f"directory-{index}").mkdir()

    with pytest.raises(ShadowPolicyError) as raised:
        scan_workspace(source, ShadowPolicy(max_files=3))

    assert raised.value.code == "shadow_limit_exceeded"
    assert raised.value.details["limit"] == "max_files"
    assert raised.value.details["maximum"] == 3
    assert raised.value.details["actual"] == 4


def test_depth_limit_hard_fails(tmp_path: Path) -> None:
    deep = tmp_path / "deep"
    (deep / "one" / "two").mkdir(parents=True)
    with pytest.raises(ShadowPolicyError) as raised:
        _manager(tmp_path / "depth-state", max_depth=1).prepare(
            run_id="depth", workspace_root=deep
        )
    assert raised.value.details["limit"] == "max_depth"


def test_symlink_limit_hard_fails_when_supported(tmp_path: Path) -> None:
    _require_symlink_capability(tmp_path)
    links = tmp_path / "links-limit"
    links.mkdir()
    (links / "target").write_text("target")
    os.symlink("target", links / "one")
    os.symlink("target", links / "two")
    with pytest.raises(ShadowPolicyError) as raised:
        _manager(tmp_path / "link-state", max_symlinks=1).prepare(
            run_id="links-limit", workspace_root=links
        )
    assert raised.value.details["limit"] == "max_symlinks"


def test_internal_symlinks_round_trip_and_unsafe_links_are_rejected(tmp_path: Path) -> None:
    _require_symlink_capability(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.txt").write_text("target")
    os.symlink("target.txt", source / "link.txt")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="links", workspace_root=source)
    assert (execution.execution_root / "link.txt").is_symlink()
    assert os.readlink(execution.execution_root / "link.txt") == "target.txt"

    external = tmp_path / "external"
    external.mkdir()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    os.symlink("../external", unsafe / "escape")
    with pytest.raises(ShadowPolicyError) as raised:
        _manager(tmp_path / "other").prepare(run_id="external", workspace_root=unsafe)
    assert raised.value.code == "shadow_unsafe_symlink"

    absolute = tmp_path / "absolute"
    absolute.mkdir()
    os.symlink(str(absolute), absolute / "absolute-link")
    with pytest.raises(ShadowPolicyError) as raised:
        _manager(tmp_path / "third").prepare(run_id="absolute", workspace_root=absolute)
    assert raised.value.code == "shadow_unsafe_symlink"


def test_windows_reparse_points_are_explicitly_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "reparse-source"
    source.mkdir()
    (source / "junction").mkdir()
    monkeypatch.setattr(
        shadow_workspace_module,
        "_is_windows_reparse",
        lambda metadata: stat.S_ISDIR(metadata.st_mode),
    )

    with pytest.raises(ShadowPolicyError) as raised:
        scan_workspace(source, ShadowPolicy())

    assert raised.value.code == "shadow_windows_reparse_point"
    assert raised.value.details["path"] == "junction"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_special_files_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")
    with pytest.raises(ShadowPolicyError) as raised:
        _manager(tmp_path).prepare(run_id="fifo", workspace_root=source)
    assert raised.value.code == "shadow_special_file"


def test_checkpoint_publish_is_idempotent_and_preserves_changes_modes_and_exclusions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "modify.txt").write_text("before")
    (source / "delete.txt").write_text("delete")
    (source / "chmod.sh").write_text("echo ok\n")
    (source / ".env").write_text("DO_NOT_TOUCH=1")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "keep.js").write_text("keep")
    if os.name != "nt":
        os.chmod(source / "chmod.sh", 0o644)
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="publish", workspace_root=source)

    (execution.execution_root / "modify.txt").write_text("after")
    (execution.execution_root / "delete.txt").unlink()
    (execution.execution_root / "new.txt").write_text("new")
    if os.name != "nt":
        os.chmod(execution.execution_root / "chmod.sh", 0o755)
    checkpoint = manager.checkpoint(execution)
    assert checkpoint["delta"]["added"] == ["new.txt"]
    assert checkpoint["delta"]["deleted"] == ["delete.txt"]
    assert checkpoint["delta"]["modified"] == ["modify.txt"]
    if os.name != "nt":
        assert checkpoint["delta"]["mode_changed"] == ["chmod.sh"]

    published = manager.publish(execution)
    assert published["status"] == "published"
    assert (source / "modify.txt").read_text() == "after"
    assert not (source / "delete.txt").exists()
    assert (source / "new.txt").read_text() == "new"
    if os.name != "nt":
        assert stat.S_IMODE((source / "chmod.sh").stat().st_mode) == 0o755
    assert (source / ".env").read_text() == "DO_NOT_TOUCH=1"
    assert (source / "node_modules" / "keep.js").read_text() == "keep"

    second = manager.publish(execution)
    assert second["status"] == "already_published"
    backup = execution.control_root / "backups" / published["publication_id"] / "manifest.json"
    assert backup.is_file()
    journal = json.loads((execution.control_root / "journal.json").read_text())
    assert journal["last_event"]["event"] in {"published", "publication_already_applied"}
    events = [json.loads(path.read_text()) for path in (execution.control_root / "journal").glob("*.json")]
    assert any(item["event"] == "published" for item in events)


@pytest.mark.skipif(os.name == "nt", reason="executable Git hook probe is POSIX-only")
def test_checkpoint_replaces_untrusted_hooks_filters_and_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base", encoding="utf-8")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="hostile-git-config", workspace_root=source)
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_text("safe", encoding="utf-8")

    hook = execution.execution_root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('hook-ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    filter_program = tmp_path / "hostile-filter.py"
    filter_program.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('filter-ran', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    hostile_filter = f"{shlex.quote(sys.executable)} {shlex.quote(str(filter_program))}"
    for arguments in (
        ("core.hooksPath", "hooks"),
        ("filter.hostile.clean", hostile_filter),
        ("filter.hostile.required", "true"),
    ):
        subprocess.run(
            [
                "git",
                "-C",
                str(execution.execution_root),
                "config",
                "--local",
                *arguments,
            ],
            check=True,
        )
    hostile_global = tmp_path / "hostile-global.gitconfig"
    hostile_global.write_text(
        "[filter \"hostile\"]\n"
        f"\tclean = {hostile_filter}\n"
        "\trequired = true\n"
        "[core]\n"
        f"\thooksPath = {hook.parent}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_global))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.hostile.process")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", hostile_filter)
    (execution.execution_root / ".gitattributes").write_text(
        "*.txt filter=hostile\n", encoding="utf-8"
    )
    (execution.execution_root / "file.txt").write_text("checkpoint", encoding="utf-8")

    checkpoint = manager.checkpoint(execution)

    assert checkpoint["private_git"]["available"] is True
    assert checkpoint["private_git"]["commit"]
    assert sentinel.read_text(encoding="utf-8") == "safe"
    filters = subprocess.run(
        [
            "git",
            "-C",
            str(execution.execution_root),
            "config",
            "--local",
            "--get-regexp",
            "^filter\\.",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert filters.returncode == 1
    hooks_path = subprocess.run(
        [
            "git",
            "-C",
            str(execution.execution_root),
            "config",
            "--local",
            "--get",
            "core.hooksPath",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(hooks_path).resolve() == (
        execution.control_root / "private-git-sandbox" / "hooks"
    ).resolve()


def test_checkpoint_unlinks_private_git_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    _require_directory_symlink_capability(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base", encoding="utf-8")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="hostile-git-symlink", workspace_root=source)
    git_root = execution.execution_root / ".git"
    external_git = tmp_path / "external-private-git"
    git_root.rename(external_git)
    sentinel = external_git / "external-sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    os.symlink(external_git, git_root, target_is_directory=True)
    (execution.execution_root / "file.txt").write_text("checkpoint", encoding="utf-8")

    checkpoint = manager.checkpoint(execution)

    assert checkpoint["private_git"]["available"] is True
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert external_git.is_dir()
    assert git_root.is_dir()
    assert not git_root.is_symlink()
    assert git_root.resolve() != external_git.resolve()


def test_checkpoint_replaces_gitdir_file_without_touching_external_repository(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base", encoding="utf-8")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="hostile-gitdir-file", workspace_root=source)
    git_root = execution.execution_root / ".git"
    external_git = tmp_path / "external-gitdir"
    git_root.rename(external_git)
    sentinel = external_git / "external-sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    git_root.write_text(f"gitdir: {external_git}\n", encoding="utf-8")
    (execution.execution_root / "file.txt").write_text("checkpoint", encoding="utf-8")

    checkpoint = manager.checkpoint(execution)

    assert checkpoint["private_git"]["available"] is True
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert external_git.is_dir()
    assert git_root.is_dir()
    assert git_root.resolve() != external_git.resolve()


def test_checkpoint_fails_closed_when_controlled_git_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base", encoding="utf-8")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="checkpoint-git-failure", workspace_root=source)
    before = json.loads((execution.control_root / "state.json").read_text())
    (execution.execution_root / "file.txt").write_text("uncommitted", encoding="utf-8")
    workspace = manager._workspace(execution)
    monkeypatch.setattr(
        workspace,
        "_git_checkpoint",
        lambda manifest: {
            "available": False,
            "commit": None,
            "reason": f"simulated-failure-{manifest[:8]}",
        },
    )

    with pytest.raises(ShadowPolicyError) as raised:
        workspace.checkpoint()

    assert raised.value.code == "shadow_private_git_unavailable"
    after = json.loads((execution.control_root / "state.json").read_text())
    assert after["status"] == before["status"] == "prepared"
    assert after["checkpoint_manifest"] == before["checkpoint_manifest"]
    assert execution.checkpoint_manifest == before["checkpoint_manifest"]
    journal = json.loads((execution.control_root / "journal.json").read_text())
    assert journal["last_event"]["event"] == "checkpoint_failed"


def test_preflight_conflict_performs_no_original_writes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.txt").write_text("base")
    (source / "user.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="conflict", workspace_root=source)
    (execution.execution_root / "agent.txt").write_text("agent")
    manager.checkpoint(execution)
    (source / "user.txt").write_text("user edit")
    before = _managed_snapshot(source)

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution)
    assert raised.value.code == "shadow_publication_conflict"
    assert raised.value.details["conflicts"] == [
        {"path": "user.txt", "reason": "original-changed"}
    ]
    assert _managed_snapshot(source) == before
    assert (source / "agent.txt").read_text() == "base"
    conflict_state = json.loads((execution.control_root / "state.json").read_text())
    assert conflict_state["status"] == "conflicted"
    assert conflict_state["last_conflict"]["conflicts"] == [
        {"path": "user.txt", "reason": "original-changed"}
    ]


def test_preflight_refuses_directory_deletion_with_excluded_content_before_writes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    protected = source / "project"
    protected.mkdir(parents=True)
    (protected / "managed.txt").write_text("managed")
    (protected / ".env").write_text("SECRET=keep")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="unmanaged-delete", workspace_root=source)
    (execution.execution_root / "project" / "managed.txt").unlink()
    (execution.execution_root / "project").rmdir()
    manager.checkpoint(execution)
    before = _managed_snapshot(source)

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution)
    assert raised.value.details["conflicts"] == [
        {"path": "project", "reason": "contains-unmanaged-content"}
    ]
    assert _managed_snapshot(source) == before
    assert (protected / "managed.txt").read_text() == "managed"
    assert (protected / ".env").read_text() == "SECRET=keep"


def test_sensitive_agent_output_fails_checkpoint_instead_of_being_omitted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="secret-output", workspace_root=source)
    (execution.execution_root / ".env").write_text("NEW_SECRET=yes")
    with pytest.raises(ShadowPolicyError) as raised:
        manager.checkpoint(execution)
    assert raised.value.code == "shadow_sensitive_output"
    assert raised.value.details["path"] == ".env"
    assert not (source / ".env").exists()


def test_restore_reconstructs_checkpoint_and_removes_uncheckpointed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="restore", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("checkpoint")
    digest = manager.checkpoint(execution)["manifest"]
    (execution.execution_root / "file.txt").write_text("lost")
    (execution.execution_root / "extra.txt").write_text("remove")
    (execution.execution_root / ".env").write_text("remove secret too")

    restored = manager.restore(execution)
    assert restored == {"ok": True, "status": "restored", "manifest": digest}
    assert (execution.execution_root / "file.txt").read_text() == "checkpoint"
    assert not (execution.execution_root / "extra.txt").exists()
    assert not (execution.execution_root / ".env").exists()
    state = json.loads((execution.control_root / "state.json").read_text())
    assert state["private_git_checkpoint"]["available"] is True
    assert state["private_git_checkpoint"]["commit"]


def test_restore_missing_tree_recreates_usable_private_git_checkpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="missing-tree", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("checkpoint")
    digest = manager.checkpoint(execution)["manifest"]
    shutil.rmtree(execution.execution_root)

    restored = manager.restore(execution)

    assert restored == {"ok": True, "status": "restored", "manifest": digest}
    assert (execution.execution_root / "file.txt").read_text() == "checkpoint"
    assert (execution.execution_root / ".git").is_dir()
    head = subprocess.run(
        [
            "git",
            "-C",
            str(execution.execution_root),
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(execution.execution_root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    state = json.loads((execution.control_root / "state.json").read_text())
    assert head == state["private_git_checkpoint"]["commit"]
    assert state["private_git_checkpoint"]["available"] is True
    assert status == ""


def test_restore_missing_tree_fails_closed_when_private_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="missing-git", workspace_root=source)
    manager.checkpoint(execution)
    shutil.rmtree(execution.execution_root)
    workspace = manager._workspace(execution)
    monkeypatch.setattr(
        workspace,
        "_git_init",
        lambda **_: {
            "available": False,
            "commit": None,
            "reason": "git-not-installed",
        },
    )

    with pytest.raises(ShadowPolicyError) as raised:
        workspace.restore()

    assert raised.value.code == "shadow_private_git_unavailable"
    assert raised.value.details == {"reason": "git-not-installed"}
    assert not execution.execution_root.exists()
    durable = json.loads((execution.control_root / "state.json").read_text())
    assert durable["status"] == "checkpointed"
    journal = json.loads((execution.control_root / "journal.json").read_text())
    assert journal["last_event"]["event"] == "restore_failed"


def test_reconciliation_can_continue_from_intact_checkpoint_when_tree_is_invalid(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="reconcile-tree", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("checkpoint")
    manager.checkpoint(execution)
    (execution.execution_root / "file.txt").write_text("partial write")
    (execution.execution_root / ".env").write_text("agent-created-sensitive-output")

    reconciliation = manager.reconciliation(execution)
    assert reconciliation["checkpoint_recoverable"] is True
    assert reconciliation["tree_matches_checkpoint"] is False
    assert reconciliation["error_code"] == "shadow_sensitive_output"
    assert "continue" in reconciliation["actions"]

    manager.restore(execution)
    assert (execution.execution_root / "file.txt").read_text() == "checkpoint"
    assert not (execution.execution_root / ".env").exists()


def test_reconciliation_does_not_offer_continue_when_checkpoint_blob_is_corrupt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="corrupt-checkpoint", workspace_root=source)
    manager.checkpoint(execution)
    blobs = [path for path in (execution.control_root / "blobs").rglob("*") if path.is_file()]
    assert blobs
    blobs[0].chmod(0o600)
    blobs[0].write_bytes(b"corrupt")

    reconciliation = manager.reconciliation(execution)
    assert reconciliation["checkpoint_recoverable"] is False
    assert reconciliation["error_code"] == "shadow_blob_corrupt"
    assert "continue" not in reconciliation["actions"]


def test_publication_resumes_after_crash_between_replace_and_journal_completion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("base a")
    (source / "b.txt").write_text("base b")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="crash", workspace_root=source)
    (execution.execution_root / "a.txt").write_text("new a")
    (execution.execution_root / "b.txt").write_text("new b")
    manager.checkpoint(execution)

    workspace = manager._workspace(execution)
    original_complete = workspace._operation_complete
    crashed = False

    def crash_once(
        state: dict[str, object],
        operation: dict[str, object],
        **_: object,
    ) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated process death")
        original_complete(state, operation)

    workspace._operation_complete = crash_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated process death"):
        workspace.publish()

    crashed_state = json.loads((execution.control_root / "state.json").read_text())
    original_backup = crashed_state["publication"]["backup_manifest"]

    reopened_manager = _manager(tmp_path)
    reopened = reopened_manager.open("crash")
    result = reopened_manager.publish(reopened)
    assert result["status"] == "published"
    resumed_state = json.loads((execution.control_root / "state.json").read_text())
    assert resumed_state["publication"]["backup_manifest"] == original_backup
    assert (source / "a.txt").read_text() == "new a"
    assert (source / "b.txt").read_text() == "new b"


def test_publication_observer_reports_durable_state_and_stable_ordinals_on_retry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("base a")
    (source / "b.txt").write_text("base b")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="observer-retry", workspace_root=source)
    (execution.execution_root / "a.txt").write_text("new a")
    (execution.execution_root / "b.txt").write_text("new b")
    manager.checkpoint(execution)

    first_events: list[tuple[str, int, dict[str, object]]] = []

    def crash_after_first_effect(
        event: str, ordinal: int, operation: object
    ) -> None:
        payload = dict(operation)  # type: ignore[arg-type]
        first_events.append((event, ordinal, payload))
        if event == "operation_completed" and ordinal == 0:
            raise RuntimeError("sqlite commit interrupted")

    with pytest.raises(RuntimeError, match="sqlite commit interrupted"):
        manager.publish(execution, observer=crash_after_first_effect)
    assert first_events[0][0] == "publication_state"
    assert [item[:2] for item in first_events[-2:]] == [
        ("operation_started", 0),
        ("operation_completed", 0),
    ]
    durable = json.loads((execution.control_root / "state.json").read_text())
    assert durable["publication"]["completed_count"] == 1
    assert durable["publication"]["active_operation"] is None
    assert (source / "a.txt").read_text() == "new a"
    assert (source / "b.txt").read_text() == "base b"

    retry_events: list[tuple[str, int, dict[str, object]]] = []

    def observe_retry(event: str, ordinal: int, operation: object) -> None:
        retry_events.append((event, ordinal, dict(operation)))  # type: ignore[arg-type]

    result = manager.publish(execution, observer=observe_retry)
    assert result["status"] == "published"
    assert retry_events[0] == (
        "publication_state",
        1,
        {
            "status": "applying",
            "completed_count": 1,
            "active_operation": None,
            "publication_id": durable["publication"]["id"],
        },
    )
    assert ("operation_started", 1) == retry_events[1][:2]
    assert ("operation_completed", 1) == retry_events[2][:2]
    assert (source / "b.txt").read_text() == "new b"


def test_publication_observer_crash_before_effect_reuses_active_ordinal(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="observer-before-effect", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("new")
    manager.checkpoint(execution)

    def crash_before_effect(event: str, ordinal: int, operation: object) -> None:
        if event == "operation_started":
            raise RuntimeError(f"crash ordinal {ordinal}")

    with pytest.raises(RuntimeError, match="crash ordinal 0"):
        manager.publish(execution, observer=crash_before_effect)
    assert (source / "file.txt").read_text() == "base"
    state = json.loads((execution.control_root / "state.json").read_text())
    assert state["publication"]["active_operation"]["ordinal"] == 0
    listed = manager.list_workspaces()
    assert listed["workspaces"][0]["marker_valid"] is True
    refused = manager.prune(candidates=[execution.run_id], eligible=lambda record: True)
    assert refused["removed"] == []
    assert refused["skipped"] == [
        {"run_id": execution.run_id, "reason": "publication-may-be-partial"}
    ]
    assert execution.shadow_root.exists()

    events: list[tuple[str, int]] = []
    result = manager.publish(
        execution,
        observer=lambda event, ordinal, operation: events.append((event, ordinal)),
    )
    assert result["status"] == "published"
    assert events[0] == ("publication_state", 0)
    assert events[1:] == [("operation_started", 0), ("operation_completed", 0)]
    assert (source / "file.txt").read_text() == "new"


def test_publication_replace_refuses_an_observer_edit_after_intent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="replace-race", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("agent")
    manager.checkpoint(execution)
    observed: list[str] = []

    def external_edit(event: str, _ordinal: int, operation: object) -> None:
        observed.append(event)
        payload = dict(operation)  # type: ignore[arg-type]
        if event == "operation_started" and payload["path"] == "file.txt":
            (source / "file.txt").write_text("external")

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution, observer=external_edit)

    assert raised.value.code == "shadow_publication_conflict"
    assert raised.value.details["conflicts"] == [
        {"path": "file.txt", "reason": "operation-target-changed"}
    ]
    assert (source / "file.txt").read_text() == "external"
    state = json.loads((execution.control_root / "state.json").read_text())
    assert state["publication"]["active_operation"]["ordinal"] == 0
    assert state["publication"]["active_operation"]["guard"]
    assert observed == ["publication_state", "operation_started"]
    journal = json.loads((execution.control_root / "journal.json").read_text())
    assert journal["last_event"]["event"] == "operation_conflict"

    # The durable active intent must retain its original guard on retry.  A
    # third-party value is never adopted as the new expected state.
    with pytest.raises(ShadowConflictError):
        manager.publish(execution)
    assert (source / "file.txt").read_text() == "external"


def test_publication_remove_refuses_an_observer_edit_after_intent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "remove.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="remove-race", workspace_root=source)
    (execution.execution_root / "remove.txt").unlink()
    manager.checkpoint(execution)

    def external_edit(event: str, _ordinal: int, operation: object) -> None:
        payload = dict(operation)  # type: ignore[arg-type]
        if event == "operation_started" and payload["path"] == "remove.txt":
            (source / "remove.txt").write_text("external")

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution, observer=external_edit)

    assert raised.value.code == "shadow_publication_conflict"
    assert (source / "remove.txt").read_text() == "external"


def test_publication_guard_detects_same_content_path_replacement(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "file.txt"
    target.write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="identity-race", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("agent")
    manager.checkpoint(execution)
    original_inode = target.stat().st_ino

    def replace_with_same_content(
        event: str, _ordinal: int, operation: object
    ) -> None:
        payload = dict(operation)  # type: ignore[arg-type]
        if event == "operation_started" and payload["path"] == "file.txt":
            replacement = tmp_path / "external-replacement"
            replacement.write_text("base")
            os.replace(replacement, target)

    with pytest.raises(ShadowConflictError):
        manager.publish(execution, observer=replace_with_same_content)

    assert target.read_text() == "base"
    if original_inode:
        assert target.stat().st_ino != original_inode


def test_publication_revalidates_each_future_path_after_an_earlier_effect(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("base a")
    (source / "b.txt").write_text("base b")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="between-operations-race", workspace_root=source)
    (execution.execution_root / "a.txt").write_text("agent a")
    (execution.execution_root / "b.txt").write_text("agent b")
    manager.checkpoint(execution)
    observed: list[tuple[str, int]] = []

    def edit_future_path(event: str, ordinal: int, _operation: object) -> None:
        observed.append((event, ordinal))
        if event == "operation_completed" and ordinal == 0:
            (source / "b.txt").write_text("external b")

    with pytest.raises(ShadowConflictError):
        manager.publish(execution, observer=edit_future_path)

    assert (source / "a.txt").read_text() == "agent a"
    assert (source / "b.txt").read_text() == "external b"
    assert ("operation_started", 1) not in observed


def test_publication_mkdir_refuses_an_observer_creation_after_intent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="mkdir-race", workspace_root=source)
    (execution.execution_root / "created").mkdir()
    manager.checkpoint(execution)

    def external_creation(event: str, _ordinal: int, operation: object) -> None:
        payload = dict(operation)  # type: ignore[arg-type]
        if event == "operation_started" and payload["path"] == "created":
            (source / "created").mkdir()
            (source / "created" / "external.txt").write_text("keep")

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution, observer=external_creation)

    assert raised.value.code == "shadow_publication_conflict"
    assert (source / "created" / "external.txt").read_text() == "keep"


def test_publication_chmod_refuses_an_observer_edit_after_intent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    script = source / "script.sh"
    script.write_text("base")
    os.chmod(script, 0o644)
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="chmod-race", workspace_root=source)
    os.chmod(execution.execution_root / "script.sh", 0o755)
    manager.checkpoint(execution)

    def external_edit(event: str, _ordinal: int, operation: object) -> None:
        payload = dict(operation)  # type: ignore[arg-type]
        if event == "operation_started" and payload["path"] == "script.sh":
            script.write_text("external")

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution, observer=external_edit)

    assert raised.value.code == "shadow_publication_conflict"
    assert script.read_text() == "external"
    assert stat.S_IMODE(script.stat().st_mode) == 0o644


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_publication_replace_refuses_parent_swap_after_intent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    parent = source / "nested"
    parent.mkdir(parents=True)
    (parent / "file.txt").write_text("base")
    outside = tmp_path / "outside"
    outside.mkdir()
    preserved = tmp_path / "preserved"
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="parent-race", workspace_root=source)
    (execution.execution_root / "nested" / "file.txt").write_text("agent")
    manager.checkpoint(execution)

    def swap_parent(event: str, _ordinal: int, operation: object) -> None:
        payload = dict(operation)  # type: ignore[arg-type]
        if event == "operation_started" and payload["path"] == "nested/file.txt":
            parent.rename(preserved)
            os.symlink(outside, parent, target_is_directory=True)

    with pytest.raises(ShadowConflictError) as raised:
        manager.publish(execution, observer=swap_parent)

    assert raised.value.code == "shadow_publication_path_unsafe"
    assert not (outside / "file.txt").exists()
    assert (preserved / "file.txt").read_text() == "base"
    state = json.loads((execution.control_root / "state.json").read_text())
    assert state["publication"]["active_operation"]["ordinal"] == 0
    journal = json.loads((execution.control_root / "journal.json").read_text())
    assert journal["last_event"]["event"] == "operation_conflict"


def test_prune_requires_terminal_state_or_explicit_durable_eligibility(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    recoverable = manager.prepare(run_id="recoverable", workspace_root=source)
    refused = manager.prune(candidates=["recoverable"])
    assert refused["removed"] == []
    assert refused["skipped"] == [
        {"run_id": "recoverable", "reason": "not-eligible"}
    ]

    selected = manager.prune(
        candidates=["recoverable"],
        eligible=lambda record: record["status"] == "prepared",
    )
    assert selected == {"ok": True, "removed": ["recoverable"], "skipped": []}
    assert not recoverable.shadow_root.exists()

    published = manager.prepare(run_id="published", workspace_root=source)
    manager.checkpoint(published)
    manager.publish(published)
    automatic = manager.prune(candidates=["published"])
    assert automatic == {"ok": True, "removed": ["published"], "skipped": []}
    assert not published.shadow_root.exists()


def test_rollback_restores_backup_and_cleanup_requires_terminal_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="rollback", workspace_root=source)
    with pytest.raises(ShadowStateError) as raised:
        manager.cleanup(execution)
    assert raised.value.code == "shadow_cleanup_unsafe"

    (execution.execution_root / "file.txt").write_text("published")
    manager.checkpoint(execution)
    manager.publish(execution)
    assert (source / "file.txt").read_text() == "published"
    rolled_back = manager.rollback(execution)
    assert rolled_back["status"] == "rolled_back"
    assert (source / "file.txt").read_text() == "base"
    cleaned = manager.cleanup(execution)
    assert cleaned["status"] == "cleaned"
    assert not execution.shadow_root.exists()


def test_discard_never_changes_original_and_removes_unpublished_shadow(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="discard", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("agent")
    manager.checkpoint(execution)
    result = manager.discard(execution)
    assert result == {"ok": True, "status": "discarded", "cleaned": True}
    assert (source / "file.txt").read_text() == "base"
    assert not execution.shadow_root.exists()


def test_cleanup_refuses_missing_or_forged_ownership_marker(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("base")
    manager = _manager(tmp_path)
    execution = manager.prepare(run_id="marker", workspace_root=source)
    (execution.execution_root / "file.txt").write_text("new")
    manager.checkpoint(execution)
    manager.publish(execution)

    marker = execution.control_root / "ownership.json"
    marker.unlink()
    with pytest.raises(ShadowStateError) as raised:
        manager.cleanup(execution)
    assert raised.value.code == "shadow_ownership_invalid"
    assert execution.shadow_root.exists()

    state = json.loads((execution.control_root / "state.json").read_text())
    marker.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "baldr-shadow-workspace",
                "run_id": execution.run_id,
                "nonce": "forged",
                "shadow_root": str(execution.shadow_root.resolve()),
                "control_root": str(execution.control_root.resolve()),
                "tree_root": str(execution.execution_root.resolve()),
            }
        )
    )
    assert state["ownership_nonce"] != "forged"
    with pytest.raises(ShadowStateError) as raised:
        manager.cleanup(execution)
    assert raised.value.code == "shadow_ownership_invalid"
    assert execution.shadow_root.exists()
