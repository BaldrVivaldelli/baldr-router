# Codex

Codex can participate in two different ways.

## Codex as a Baldr provider

This is the default implementation path:

```text
MCP client → Baldr Router → Codex CLI
```

Baldr uses `codex exec --json`, structured output, telemetry, and role-specific sandbox policy. Optional app-server and SDK runners remain experimental.

## Codex as an MCP client

Codex can also connect to Baldr Router itself. See:

```text
facades/generic-mcp/codex/config.toml.example
```

Avoid recursive topology in which a child Codex provider invokes Baldr Router again. Baldr marks child executions with anti-reentry environment metadata and providers should not self-delegate.
