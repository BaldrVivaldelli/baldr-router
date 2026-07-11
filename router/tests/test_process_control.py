from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baldr_router.process_control import active_processes, managed_popen, terminate_process_tree
from baldr_router.run import run_command


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_terminate_process_tree_kills_descendant(tmp_path: Path):
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        """
import subprocess, sys, time
from pathlib import Path
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(p.pid))
time.sleep(60)
""".strip(),
        encoding="utf-8",
    )
    proc = managed_popen(
        [sys.executable, str(script), str(child_pid_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 5
    while not child_pid_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    assert _pid_exists(proc.pid)
    assert _pid_exists(child_pid)

    cleanup = terminate_process_tree(proc, grace_seconds=0.2)
    deadline = time.time() + 3
    while (_pid_exists(proc.pid) or _pid_exists(child_pid)) and time.time() < deadline:
        time.sleep(0.05)

    assert cleanup["terminated"] is True
    assert not _pid_exists(proc.pid)
    # A killed child can briefly remain as a zombie until init reaps it. Treat
    # a /proc zombie as terminated as well.
    stat_path = Path(f"/proc/{child_pid}/stat")
    if stat_path.exists():
        assert stat_path.read_text().split()[2] == "Z"
    else:
        assert not _pid_exists(child_pid)
    assert active_processes() == []


def test_run_command_timeout_returns_structured_cleanup():
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=1,
    )

    assert result["ok"] is False
    assert result["exit_code"] == 124
    assert result["error"]["code"] == "timeout"
    assert result["cleanup"]["terminated"] is True
    assert active_processes() == []
