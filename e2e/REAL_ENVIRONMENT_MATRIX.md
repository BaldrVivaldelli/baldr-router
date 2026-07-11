# v0.16.1 real-environment qualification matrix

A mandatory profile passes only after **three consecutive clean runs** from a restored snapshot or clean installation. Record the Baldr evidence ID for every run.

| Profile | Machine/client | Run 1 evidence | Run 2 evidence | Run 3 evidence | Result | Notes |
|---|---|---|---|---|---|---|
| VS Code Windows + automatic WSL fallback |  |  |  |  | Pending |  |
| VS Code Remote WSL direct runtime |  |  |  |  | Pending |  |
| Linux native + VS Code |  |  |  |  | Pending |  |
| Kiro Power + adapter + WSL |  |  |  |  | Pending |  |

## Required lifecycle assertions

Every profile must prove:

1. clean install/bootstrap;
2. MCP handshake and frozen tool/prompt surface;
3. untrusted workspace performs no profile scan or provider execution;
4. trusted workspace produces a bounded profile;
5. deterministic execute and ordered progress;
6. cancellation terminates parent and descendant processes;
7. router crash/restart recovers;
8. v0.15 → v0.16 upgrade preserves config/secrets and migrates SQLite with backup;
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

After the lifecycle matrix passes, run the ten frozen canaries: five bounded tasks in one real Python repository and five in one real Node repository. Record run IDs, evidence IDs, test/verification references, zero orphan processes, and all required per-task invariants. Cancellation, crash recovery, reconciliation, upgrade, fencing, and secret redaction are lifecycle/client assertions rather than duplicated inside every coding canary. Synthetic lab results do not count as real-client passes.


## Machine-readable receipt

The Markdown matrix is a human index. The authoritative result is the generated qualification receipt:

```bash
baldr-router qualification status --latest --qualified-only
```

Every row should record the qualification ID and receipt SHA-256, not only screenshots.
