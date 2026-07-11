import json
from pathlib import Path

from baldr_router.context7 import detect_workspace_libraries


def test_detect_workspace_libraries_from_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "next": "latest",
                    "react": "latest",
                    "left-pad": "latest",
                }
            }
        )
    )
    libs = detect_workspace_libraries(tmp_path, "fix next middleware auth", limit=3)
    assert "next" in libs
