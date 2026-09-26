from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# scripts/dev.py already isolates Git and XDG before invoking pytest, but the
# documented developer command runs pytest directly. Keeping the guarantee here
# instead of only in the wrapper means a disposable repository never inherits a
# developer's signing key, hooks or global Git policy, and a durable test never
# writes into the real Baldr state directory.
_GIT_CONFIG = "[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n"


@pytest.fixture(scope="session", autouse=True)
def isolated_environment() -> Iterator[None]:
    with tempfile.TemporaryDirectory(prefix="baldr-test-state-") as temp:
        root = Path(temp)
        git_config = root / "gitconfig"
        git_config.write_text(_GIT_CONFIG, encoding="utf-8")
        overrides = {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        # An outer runner that already isolated the environment stays
        # authoritative, so dev.py and CI keep one disposable root per run.
        applied = {
            key: value for key, value in overrides.items() if key not in os.environ
        }
        os.environ.update(applied)
        try:
            yield
        finally:
            for key in applied:
                os.environ.pop(key, None)
