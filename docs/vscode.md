# VS Code integration

## Recommended: native extension

The native extension in `facades/vscode-extension/` provides **Instalación A UN SOLO clic**:

- bundles the `baldr-router` wheel;
- prepares a private Python environment automatically;
- registers Baldr as an MCP server programmatically;
- detects host, Windows/WSL, and Remote WSL;
- stores an optional Context7 key in VS Code SecretStorage;
- contributes a dedicated Baldr Activity Bar console with durable work items;
- exposes one Command Palette entry and keeps chat as an optional shortcut;
- renders workflow phases, cancellation, reconciliation, evidence, and resolved execution profiles.

Install the generated artifact:

```text
dist/baldr-router-vscode-0.17.0.vsix
```

Then use the dedicated view:

```text
Activity Bar -> Baldr
  type a task and press Enter
  + for attachments and configuration
  /new /run /status /profile /git /context /cancel /resume
```

Optional shortcuts remain available:

```text
Baldr: Open
@baldr /setup
@baldr /status
@baldr /run <task>
@baldr <task>
```

No workspace `mcp.json`, global npm launcher, manual Python package installation, or configuration form is needed for this path. See [`baldr-console.md`](baldr-console.md).

Provider authentication and explicit trust dialogs remain provider/VS Code security steps, not manual Baldr configuration.

## Agent Plugin facade (Preview)

`facades/vscode-agent-plugin/` bundles:

```text
/baldr-setup
/baldr-status
/baldr-run <task>
```

and a thin `.mcp.json` pointing to `baldr-router-launcher`. It intentionally contains no routing/workflow implementation.

## Generic MCP configuration

For development or users who do not want the extension:

```json
{
  "servers": {
    "baldrRouter": {
      "type": "stdio",
      "command": "baldr-router-launcher",
      "args": ["mcp"]
    }
  }
}
```

When VS Code itself runs inside Remote WSL and sees the router directly:

```json
{
  "servers": {
    "baldrRouter": {
      "type": "stdio",
      "command": "baldr-router",
      "args": ["mcp"]
    }
  }
}
```
