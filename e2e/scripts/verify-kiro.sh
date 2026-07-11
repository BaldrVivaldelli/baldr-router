#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${1:-$PWD}"
PROFILE="kiro-windows-wsl"

command -v baldr-router
command -v baldr-kiro-adapter
baldr-router --version
baldr-router extensions
baldr-router workflow-status
baldr-kiro-adapter --help >/dev/null

if ! git -C "$WORKSPACE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'Kiro adapter validation requires a trusted Git workspace: %s\n' "$WORKSPACE" >&2
  exit 2
fi

baldr-router trust-workspace "$WORKSPACE"
baldr-router probe-workspace "$WORKSPACE" --refresh
baldr-router verify "$WORKSPACE" --mode full --client "$PROFILE"
baldr-router lab "$WORKSPACE" --mode full --repeat 3 --profile "$PROFILE"
baldr-router evidence --latest

printf '\nAutomated Kiro adapter/runtime checks completed. Open Kiro, install/reload the Power, execute the UI workflow in e2e/kiro-power.md, and record the evidence IDs in e2e/REAL_ENVIRONMENT_MATRIX.md.\n'
