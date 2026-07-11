param(
  [string]$Distro = "",
  [string]$Workspace = ""
)

$ErrorActionPreference = 'Stop'

wsl.exe --status
wsl.exe --list --verbose

$distroArgs = @()
if ($Distro) {
  $distroArgs = @('-d', $Distro)
}

function Invoke-WslBash {
  param([Parameter(Mandatory = $true)][string]$Command)
  & wsl.exe @distroArgs -- bash -lc $Command
  if ($LASTEXITCODE -ne 0) {
    throw "WSL command failed with exit code $LASTEXITCODE: $Command"
  }
}

Invoke-WslBash 'command -v python3; command -v git; command -v codex; command -v baldr-router; codex login status; baldr-router --version; baldr-router env-report; baldr-router extensions; baldr-router workflow-status'

$workspaceClause = ''
if ($Workspace) {
  $escapedWorkspace = $Workspace.Replace("'", "'\"'\"'")
  $workspaceClause = "WORKSPACE='$escapedWorkspace'; if git -C \"`$WORKSPACE\" rev-parse --is-inside-work-tree >/dev/null 2>&1; then baldr-router trust-workspace \"`$WORKSPACE\"; baldr-router probe-workspace \"`$WORKSPACE\" --refresh; WORKSPACE_ARG=\"`$WORKSPACE\"; else echo 'Workspace is not a Git repo; using fixture-only lifecycle validation.'; WORKSPACE_ARG=''; fi;"
} else {
  $workspaceClause = "WORKSPACE_ARG='';"
}

$validation = @"
set -euo pipefail;
$workspaceClause
if [ -n "`$WORKSPACE_ARG" ]; then
  baldr-router verify "`$WORKSPACE_ARG" --mode full --client vscode-windows-wsl --include-provider-smoke;
  baldr-router lab "`$WORKSPACE_ARG" --mode full --repeat 3 --profile vscode-windows-wsl;
else
  baldr-router verify --mode full --client vscode-windows-wsl --include-provider-smoke;
  baldr-router lab --mode full --repeat 3 --profile vscode-windows-wsl;
fi;
baldr-router evidence --latest
"@

Invoke-WslBash $validation
Write-Host "Automated Windows + WSL checks completed. Open Windows VS Code, finish e2e/vscode-windows-wsl.md, and record the evidence IDs in e2e/REAL_ENVIRONMENT_MATRIX.md."
