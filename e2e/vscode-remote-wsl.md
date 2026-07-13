# E2E: VS Code Remote WSL

1. Open a disposable Git repository through **WSL: Open Folder in WSL**.
2. Install the Baldr extension in the WSL extension host.
3. Run `@baldr /setup`; confirm the runtime target is a direct Linux/host target, not `wsl.exe`.
4. Run `@baldr /status` and verify the workspace trust root matches the Linux path.
5. Run one harmless `/run` task and verify architect, implementer, and reviewer steps.
6. Cancel a long task and check `ps -ef` for orphaned provider processes.
7. Reload the VS Code window and verify the private runtime is reused only when its v0.19 manifest and wheel hash match.

Pass when no Windows path translation or bridge is involved and all lifecycle checks succeed.
