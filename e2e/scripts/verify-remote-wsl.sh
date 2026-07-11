#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${1:-$PWD}"
PROFILE="vscode-remote-wsl"

command -v python3
command -v git
command -v codex
command -v baldr-router
codex login status
baldr-router --version
baldr-router env-report
baldr-router extensions
baldr-router workflow-status

if git -C "$WORKSPACE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  baldr-router trust-workspace "$WORKSPACE"
  baldr-router probe-workspace "$WORKSPACE" --refresh
  WORKSPACE_ARG=("$WORKSPACE")
else
  printf '\nWorkspace is not a Git repository; lifecycle verification will use only the temporary fixture repo.\n'
  WORKSPACE_ARG=()
fi

# One real, read-only provider smoke validates the authenticated Codex boundary.
baldr-router verify "${WORKSPACE_ARG[@]}" \
  --mode full \
  --client "$PROFILE" \
  --include-provider-smoke

# The deterministic suite must pass three consecutive times without spending provider credits.
baldr-router lab "${WORKSPACE_ARG[@]}" \
  --mode full \
  --repeat 3 \
  --profile "$PROFILE"

baldr-router evidence --latest
printf '\nAutomated Remote WSL checks completed. Finish the UI assertions in e2e/vscode-remote-wsl.md and record the evidence IDs in e2e/REAL_ENVIRONMENT_MATRIX.md.\n'
