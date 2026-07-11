# Generic MCP facades

These examples connect clients directly to `baldr-router` without a native adapter.

The shared user-facing intents are exposed as MCP prompts:

```text
setup
status
run
```

The same implementation is available through MCP tools and through:

```bash
baldr-router facade setup [workspace]
baldr-router facade status [workspace]
baldr-router facade run <workspace> <task>
```

Use the native VS Code extension for **Instalación A UN SOLO clic**, secure Context7 storage, and automatic runtime bootstrap. Generic configs assume `baldr-router` or `baldr-router-launcher` is already available.
