# Clean-machine installation

The VS Code facade is designed as **Instalación A UN SOLO clic con bootstrap automático**: installing the extension registers the MCP server, discovers the correct host/WSL environment, and prepares a private versioned Baldr runtime. Provider authentication and editor trust prompts remain explicit security consent, not manual configuration.

## Prerequisites

At least one supported Python 3 interpreter must be available on the execution target:

- Windows host, Linux, or macOS: Python 3.11+
- Windows with Baldr in WSL: Python 3.11+ inside the selected WSL distribution
- VS Code Remote WSL: Python 3.11+ in the remote distribution

The provider used by the default workflow must also be authenticated:

```bash
codex login
codex login status
```

Context7 is optional and is configured during `/setup`.

## VS Code: clean Windows machine with optional WSL

1. Install the `baldr-router-vscode-0.19.0.vsix`, or install Baldr Router from the Marketplace when published.
2. Accept VS Code's publisher and MCP trust dialogs.
3. Open the folder you want BALDR to work on; Git is optional under Automatic protection.
4. Open the **Baldr** icon in the Activity Bar. The dedicated console prepares status and setup; `@baldr /setup` remains an optional shortcut.
5. The extension tries a private Windows runtime first. If Windows cannot provide a compatible runtime and WSL is available, it installs/uses the private WSL runtime automatically.
6. Complete `codex login` in the environment reported by Baldr if status requests it.
7. Optionally store a Context7 key through the secure input. The extension uses VS Code `SecretStorage`; it does not write the key to the workspace or Baldr TOML.

No `.vscode/mcp.json`, global launcher, `uv tool install`, or manual WSL bridge is required.

## VS Code Remote WSL

1. Open the repository with **WSL: Open Folder in WSL**.
2. Install the extension in the WSL extension host when VS Code asks where to install it.
3. Run `@baldr /setup`.
4. Baldr prepares the private runtime directly inside the remote Linux environment; no Windows bridge is used.

## Kiro

Kiro uses the optional facade rather than the VS Code extension:

1. Install the core and adapter into the same environment:

   ```bash
   uv tool install --force ./router --with ./facades/kiro/adapter
   ```

2. Install the Power from `facades/kiro/baldr-orchestrator/`.
3. Start the shared setup intent. The adapter trusts the current Git workspace only after the setup action and creates the managed hook idempotently.
4. Confirm `router_extension_status` lists the Kiro adapter.

## Generic MCP clients

Clients without a native facade can launch:

```text
baldr-router mcp
```

Use the examples in `facades/generic-mcp/`. A generic client must pass or persist an explicit trusted workspace before running providers.

## Upgrade behavior

Each VS Code extension version installs its wheel into a versioned private runtime. The bootstrap:

1. verifies the wheel SHA-256 against `runtime.json`;
2. installs into a temporary directory;
3. creates the virtual environment at its final absolute path and restores the previous same-version runtime if installation or verification fails;
4. keeps the current runtime and one recent prior runtime by default;
5. never reuses a runtime whose manifest version or wheel hash does not match.

A failed upgrade therefore leaves a previously working version intact.

## Uninstallation and local data

The extension can be removed from VS Code normally. Baldr's user data may remain intentionally:

- config: `~/.config/baldr-router/`
- telemetry: `~/.local/state/baldr-router/`
- Context7 cache: `~/.cache/baldr-router/`
- VS Code private runtime: extension global storage / `.baldr-router-vscode`

Delete those directories only when you also want to remove local settings, trust roots, telemetry, and caches.
