from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from baldr_router.qualification.runner import run_qualification
from baldr_router.workspace_policy import trust_workspace


def _portable_text(value: str) -> str:
    result = value
    replacements = {
        str(Path.home()): "~",
        str(Path.cwd()): "<checkout>",
    }
    for source, target in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        if source:
            result = result.replace(source, target)
    return result


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def export_result(result: dict[str, Any], output_directory: str | Path) -> dict[str, Any]:
    """Export a portable CI summary and the already-redacted qualification bundle."""
    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    bundle = result.get("bundle") or {}
    bundle_path = Path(str(bundle.get("path") or "")).expanduser()
    receipt_target = output / "receipt"
    if bundle_path.is_dir():
        shutil.copytree(bundle_path, receipt_target)

    summary = {
        "ok": result.get("ok") is True,
        "status": str(result.get("status") or "failed"),
        "qualification_id": result.get("qualification_id"),
        "profile": result.get("profile"),
        "receipt_sha256": result.get("receipt_sha256"),
        "checks": result.get("checks") or {},
        "next_steps": result.get("next_steps") or [],
        "receipt_directory": "receipt" if receipt_target.is_dir() else None,
    }
    _write_json(output / "qualification-result.json", summary)
    return {**summary, "output_directory": str(output)}


def run(args: argparse.Namespace) -> int:
    output = Path(args.output_directory).expanduser().resolve()
    try:
        trust = trust_workspace(args.workspace_root)
        if not trust.get("ok"):
            raise RuntimeError(f"Workspace trust failed: {trust.get('reason') or trust}")
        result = run_qualification(
            profile_id=args.profile,
            workspace_root=args.workspace_root,
            client_assertions_path=Path(args.evidence_directory) / "client-assertions.json",
            canary_results_path=Path(args.evidence_directory) / "canary-results.json",
            repeat=args.repeat,
            include_provider_smoke=not args.no_provider_smoke,
            client_id=args.client,
        )
        exported = export_result(result, output)
        print(json.dumps(exported, indent=2, sort_keys=True))
        return 0 if result.get("status") == "qualified" else 2
    except Exception as exc:
        output.mkdir(parents=True, exist_ok=True)
        failure = {
            "ok": False,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": _portable_text(str(exc)),
        }
        _write_json(output / "qualification-error.json", failure)
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 3


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Cross-platform self-hosted real-environment qualification entrypoint"
    )
    value.add_argument("--profile", required=True)
    value.add_argument("--workspace-root", required=True)
    value.add_argument("--evidence-directory", required=True)
    value.add_argument("--output-directory", default="qualification-output")
    value.add_argument("--repeat", type=int, default=3)
    value.add_argument("--client")
    value.add_argument("--no-provider-smoke", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
