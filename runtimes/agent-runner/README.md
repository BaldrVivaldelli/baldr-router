# Baldr Agent Runner

`baldr-agent-runner` is a separate local data-plane process. It executes an
externally owned, digest-pinned agent near an explicitly granted workspace and
speaks `baldr-agent-execution` v1 over JSONL stdio.

It does not discover agents, choose teams or contain product agent code. Those
responsibilities remain with Agent Manager, Baldr Router and the external
repository respectively.

Version 1 supports:

- immutable manifest and artifact verification;
- idempotent jobs in a private SQLite store;
- health, invoke, status, event pagination and cancellation messages;
- a disposable snapshot for read-only jobs;
- direct, explicitly granted workspace access for the single write participant;
- bounded process execution, timeouts and process-tree cancellation.

From the Baldr monorepo:

```bash
uv tool install --force --editable ./runtimes/agent-runner \
  --with-editable ./sdks/python
baldr-agent-runner health
```

See [`docs/external-agent-runtime.md`](../../docs/external-agent-runtime.md)
for manifests, registration, security semantics and transport compatibility.
