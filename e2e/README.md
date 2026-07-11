# Real-environment E2E validation

Automated tests use deterministic fixture workers and simulated platform discovery. The scripts in this directory add **Baldr Probe**, **Baldr Verify**, and the three-pass **Baldr Lab** to the actual target environment before the UI-specific checks.

Runbooks:

- `vscode-windows-wsl.md`
- `vscode-remote-wsl.md`
- `kiro-power.md`
- `REAL_ENVIRONMENT_MATRIX.md`

Helpers:

```text
e2e/scripts/verify-vscode-windows-wsl.ps1
e2e/scripts/verify-remote-wsl.sh
e2e/scripts/verify-kiro.sh
```

The helpers perform a bounded environment/workspace probe, inspect the durable SQLite schema and resolved execution profiles, run one full lifecycle verification, require three consecutive deterministic lab runs, and print the latest evidence bundle. Durable crash/restart assertions are additionally covered by the core transition test suite and should be spot-checked in the target client. The VS Code helpers also run one optional real Codex read-only smoke when authentication is available.

Examples:

```powershell
# Windows PowerShell. Workspace is a Linux path inside the selected WSL distro.
.\e2e\scripts\verify-vscode-windows-wsl.ps1 -Distro Ubuntu -Workspace /home/me/projects/fixture
```

```bash
./e2e/scripts/verify-remote-wsl.sh /home/me/projects/fixture
./e2e/scripts/verify-kiro.sh /home/me/projects/fixture
```

Record every evidence ID in a copy of `REAL_ENVIRONMENT_MATRIX.md`. Synthetic lab results are necessary but do not replace the client UI assertions or real-provider canary. Never mark a live scenario passed from synthetic tests alone.


Generate and evaluate the machine-readable templates with `baldr-router qualification`. See `docs/real-environment-qualification.md`. A profile is not qualified until all client assertions and ten real-repository canaries are present.
