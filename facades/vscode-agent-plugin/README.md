# Baldr Router Agent Plugin for VS Code

Preview facade for VS Code Agent Plugins. It contains only:

- slash commands generated from the shared `setup`, `status`, and `run` contract;
- an MCP declaration that points to the same `baldr-router` runtime.

It does **not** reimplement providers, role selection, workflows, Context7, telemetry, or verification.

## Commands

```text
/baldr-setup
/baldr-status
/baldr-run <task>
```

## Runtime prerequisite

The Agent Plugin facade expects `baldr-router-launcher` to be available. The native VS Code extension under `../vscode-extension/` is the recommended **Instalación A UN SOLO clic** path because it bundles and prepares the Python runtime automatically.

Agent Plugin support is a secondary facade while the VS Code feature remains in Preview.
