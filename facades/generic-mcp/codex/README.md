# Codex client adapter

This example lets Codex use Baldr Router as an MCP server.

That gives Codex access to router tools such as telemetry, Context7 cache, or future provider orchestration.

This is optional. The default architecture is usually:

```text
MCP client -> baldr-router -> Codex provider
```

This adapter enables a different direction:

```text
Codex -> baldr-router MCP tools
```

Use `config.toml.example` as a starting point for `~/.codex/config.toml`.
