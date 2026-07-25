from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"baldr_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_release = _load_script("build_release")
release_metadata = _load_script("release_metadata")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git("init", "-q", cwd=root)
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("release input\n", encoding="utf-8")
    (root / "untracked.txt").write_text("user input\n", encoding="utf-8")
    ignored = root / "ignored"
    ignored.mkdir()
    (ignored / "runtime.txt").write_text("downloaded runtime\n", encoding="utf-8")
    _git("add", "--", ".gitignore", "tracked.txt", cwd=root)
    return root


def test_source_zip_contains_only_tracked_repository_inputs(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    target = tmp_path / "source.zip"

    original_root = build_release.ROOT
    build_release.ROOT = root
    try:
        build_release.zip_directory(
            root,
            target,
            root_name="project",
            tracked_only=True,
        )
    finally:
        build_release.ROOT = original_root

    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {
            "project/.gitignore",
            "project/tracked.txt",
        }


def test_secret_scan_ignores_untracked_user_inputs(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    synthetic_secret = "token = " + "x" * 32
    (root / "untracked.txt").write_text(synthetic_secret + "\n", encoding="utf-8")
    output = tmp_path / "secret-scan.json"

    result = release_metadata.secret_scan(output, root=root)

    assert result["ok"] is True
    assert result["files_scanned"] == 2
    assert result["findings"] == []
