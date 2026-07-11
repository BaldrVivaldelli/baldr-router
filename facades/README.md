# Client facades

Baldr Router is MCP-first. Every directory here is a thin, optional facade over the same versioned [`facade-v1`](../contracts/facade-v1.json) contract.

```text
vscode-extension/
  Primary native distribution. Instalación A UN SOLO clic, managed private runtime,
  programmatic MCP registration, SecretStorage, Baldr: Open, and @baldr.

vscode-agent-plugin/
  Preview declarative plugin with /baldr-setup, /baldr-status, /baldr-run,
  one thin skill, and an MCP declaration.

kiro/adapter/
  Installable Kiro-only extension that owns workspace hooks and idempotent onboarding.

kiro/baldr-orchestrator/
  Kiro Power facade over the same setup/status/run contract.

generic-mcp/
  Minimal configuration examples for VS Code, Codex, and Claude Desktop.
```

Facades may own client-native UI, secure secret storage, runtime discovery, installation, and client-specific hooks. They must not duplicate provider selection, role assignment, workflow execution, Context7 enrichment, telemetry, or verification logic.
