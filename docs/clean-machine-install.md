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

1. Install the `baldr-router-vscode-0.20.0.vsix`, or install Baldr Router from the Marketplace when published.
2. Accept VS Code's publisher and MCP trust dialogs.
3. Open the Git repository you want Baldr to work on. Direct work is the default; a non-Git folder remains an explicit reduced-guarantee choice.
4. Once startup finishes, Baldr prepares its private runtime in the background
   and offers to open the console. You can also open the **Baldr** icon in the
   Activity Bar; `@baldr /setup` remains an optional shortcut.
5. The extension tries a private Windows runtime first. If Windows cannot provide a compatible runtime and WSL is available, it installs/uses the private WSL runtime automatically.
6. Complete `codex login` in the environment reported by Baldr if status requests it.
7. Optionally store a Context7 key through the secure input. The extension uses VS Code `SecretStorage`; it does not write the key to the workspace or Baldr TOML.

No `.vscode/mcp.json`, global launcher, `uv tool install`, or manual WSL bridge is required.

The complete first-use and external-agent journey is documented in
[`golden-path.md`](golden-path.md).

## VS Code Remote WSL

1. Open the repository with **WSL: Open Folder in WSL**.
2. Install the extension in the WSL extension host when VS Code asks where to install it.
3. Run `@baldr /setup`.
4. Baldr prepares the private runtime directly inside the remote Linux environment; no Windows bridge is used.

## Kiro

Kiro uses the optional facade rather than the VS Code extension. Its packages
and compatibility tests remain part of v0.20, but its real-client
qualification is explicitly deferred and cannot block or satisfy this
iteration's VS Code Remote WSL + Codex promotion gate:

1. Download and extract `baldr-router-0.20.0-artifacts.zip`.
2. From the extracted directory, install the core and adapter into the same
   environment where Router will run (the host or the selected WSL
   distribution):

   ```bash
   cd artifacts/python
   uv tool install --force ./baldr_router-0.20.0-py3-none-any.whl \
     --with ./baldr_kiro_adapter-0.20.0-py3-none-any.whl \
     --with-executables-from baldr-kiro-adapter
   cd ../..
   ```

3. On the host that starts Kiro and its MCP servers—normally Windows
   PowerShell—install the launcher shipped in the same release and verify that
   it resolves Router `0.20.0`:

   ```bash
   npm install --global \
     ./artifacts/node/baldr-router-launcher-0.20.0.tgz
   baldr-router-launcher detect
   ```

4. Extract `artifacts/baldr-orchestrator-kiro-0.20.0.zip`. In Kiro, use
   **Powers → Add Custom Power → Import power from a folder → Install** and
   select the resulting `baldr-orchestrator/` directory. Do not copy the
   directory into `~/.kiro/powers/installed`: the supported install action is
   what registers the namespaced MCP entry under `powers.mcpServers`.
5. Start the shared setup intent. The adapter trusts the current Git workspace only after the setup action and creates the managed hook idempotently.
6. Confirm `router_extension_status` lists the Kiro adapter.

The release does not depend on an npm registry or a source checkout: the
launcher tarball, Power ZIP, Router wheel, and adapter wheel are all contained
in the artifact bundle.

If **Kiro - MCP Logs** reports `excluded by registry-only access mode`, an
organization policy is rejecting every local/non-registry MCP server. This is
not a launcher fallback condition. Ask the Kiro administrator to allow local
Powers or publish/allow Baldr in the organization's MCP Registry, or qualify
with a profile where local Powers are permitted. Baldr must not bypass the
client's enterprise policy.

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
