# E2E: VS Code on Windows with automatic WSL bridge

## Preconditions

- VS Code runs as a Windows application, not Remote WSL.
- Ubuntu or another WSL distribution is installed.
- Codex is installed and authenticated in the intended target environment.
- Open a small disposable Git repository.

## Steps

1. Install `baldr-router-vscode-0.19.0.vsix` over any previous version.
2. Reload VS Code once if requested by VS Code.
3. Run `Baldr: Open` → **Status** before setup. Confirm the runtime target is either the Windows host or WSL and no hand-written `mcp.json` is required.
4. Run `@baldr /setup`. Confirm the open repository is reported as trusted and is a Git repository.
5. Run `@baldr /status`. Confirm Codex is found/authenticated and the frozen workflow is listed.
6. Run `@baldr /run Create a harmless file named baldr-e2e.txt containing one line, then review it.`
7. Inspect the diff and final structured report.
8. Start a deliberately long task, cancel it from the VS Code chat progress UI, and verify no `codex`, `kiro-cli`, or child shell from that run remains.
9. Configure Context7 with a disposable/rotated key, open **Baldr Router** output, and verify the key does not appear. Remove or rotate the test key afterward.
10. Reinstall the same VSIX. Confirm setup/status still work and the runtime manifest remains version/hash valid.

## Pass conditions

- Automatic host/WSL selection succeeds.
- No manual PATH, MCP JSON, or WSL command is required.
- The task produces a reviewed diff and structured result.
- Cancellation terminates the process tree.
- No secret appears in Output, telemetry, cache metadata, or workspace files.
