# baldr-kiro-adapter

Optional Kiro-only extension for the client-agnostic Baldr Router MCP core.

It owns:

- idempotent `.kiro/hooks/` materialization;
- safe conflict detection and backups;
- Kiro workspace status/install/uninstall tools;
- deprecated `delegate_spec_task` compatibility alias;
- a redacted Kiro client/runtime receipt used by real-environment qualification.

It does not implement providers, routing, workflows, Context7, telemetry, or verification.

Install with the core in the same tool environment:

```bash
uv tool install --force --editable ./router \
  --with-editable ./facades/kiro/adapter \
  --with-executables-from baldr-kiro-adapter
```
