# v0.20.0 real-environment qualification matrix

The v0.20 promotion profile passes only after **three consecutive clean runs**
from a restored snapshot or clean installation. Record the Baldr evidence ID
for every run. Other profiles remain useful compatibility evidence but do not
block this Codex-focused iteration.

| Profile | Promotion scope | Machine/client | Run 1 evidence | Run 2 evidence | Run 3 evidence | Result | Notes |
|---|---|---|---|---|---|---|---|
| VS Code Remote WSL direct runtime + Codex | Required | VS Code 1.126 / WSL Ubuntu | `br-verify-93e5995cd4a7` | `br-verify-b4f7672e4507` | `br-verify-7c65b9d4b20e` | In progress | Provisional `br-qualification-vscode-remote-wsl-20260719T173455Z-2690fd65` / Lab `br-lab-20260719T173455Z-e57c5a77d6`: environment, three-pass lab, three Codex provider smokes, all ten independent reconciliation actions and 10/10 real canaries passed. The receipt records 30/35 assertions after observing the attention action and exact changed-file navigation. Five UI observations and a new receipt remain. |
| VS Code Windows + automatic WSL fallback | Deferred |  |  |  |  | Not blocking | Compatibility and runbook retained. |
| Linux native + VS Code | Deferred |  |  |  |  | Not blocking | Independently qualifiable. |
| Kiro Power + adapter + WSL | Future iteration |  |  |  |  | Not blocking | Implementation, packaging and tests retained; real Kiro UI qualification is deferred. |

## Required lifecycle assertions

A profile promoted as `qualified` must prove:

1. clean install/bootstrap;
2. MCP handshake and frozen tool/prompt surface;
3. untrusted workspace performs no profile scan or provider execution;
4. trusted workspace produces a bounded profile;
5. deterministic execute and ordered progress;
6. cancellation terminates parent and descendant processes;
7. router crash/restart recovers;
8. v0.19 → v0.20 upgrade preserves config/secrets and migrates SQLite with backup;
9. interrupted upgrade rolls back;
10. uninstall/reinstall works and a secret scan is clean;
11. SQLite schema is current and the database is on the runtime-local filesystem;
12. architecture/implementation/review show the expected resolved execution profiles;
13. a read-only crash resumes from the durable boundary without re-running completed steps;
14. a write-side-effect crash becomes `unknown`/`awaiting_reconciliation` rather than retrying blindly;
15. provider sessions remain separated by role/model/profile and a compatible workspace-scoped session can resume;
16. a stale worker cannot persist after a lease-epoch takeover;
17. the same idempotency key with a different request returns `idempotency_conflict`;
18. cancellation reaches durable `cancelled` with zero orphan processes;
19. worktree reconstruction and every allowed reconciliation action are exercised;
20. SQLite quick/integrity checks, backup, GC and session expiry pass.

## Real task canary

After the lifecycle matrix passes, run the ten frozen operational canaries: in
a real Python repository, a normal change, a tested change, UI cancellation,
read-only recovery and write reconciliation; in a distinct real Node
repository, publication conflict, upgrade preservation, session reuse, lease
fencing and secret redaction. Use disposable clones or restored snapshots.
Record run IDs, evidence IDs, test/verification references, zero orphan
processes and every required per-task invariant. The Lab supplies supporting
contract evidence but never replaces a real-client canary run.


## Machine-readable receipt

The Markdown matrix is a human index. The authoritative result is the generated qualification receipt:

```bash
baldr-router qualification status --latest --qualified-only
```

Every row should record the qualification ID and receipt SHA-256, not only screenshots.
