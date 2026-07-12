# Baldr Router for VS Code

Native VS Code facade for the client-agnostic **Baldr Router** MCP runtime.

## Primary experience: Baldr Console

The extension contributes its own **Baldr** section to the Activity Bar. Copilot Chat remains optional.

```text
Baldr
  -> durable task list
  -> selected-item timeline
  -> fixed composer
  -> + menu
  -> slash autocomplete
  -> Git / preset / role / Context7 chips
```

There is no mandatory setup form. Type a task and press Enter; Baldr creates a durable work item and runs it using the active workspace preferences.

### Slash commands

```text
/new <task>
/run [task]
/status
/profile <fast|balanced|deep|custom>
/git <worktree|current|off>
/context <auto|on|off>
/roles
/cancel
/resume
/archive
/setup
/help
```

The commands are aliases over the existing `setup`, `status`, and `run` facade contract.

### `+` menu

Use `+` to create an item, attach the active file or selection, choose a file/folder, switch preset or Git mode, configure Context7, choose role profiles, or open logs.

### Git modes

```text
Git worktree       recommended, isolated, recoverable
Current workspace  in-place, clean Git repository required
Non-Git            explicit consent, reduced recovery guarantees
```

The extension never silently disables Git safety.

## Durable state

Tasks are persisted by the core in SQLite, not in the webview. Closing VS Code or restarting Baldr does not erase the work-item list. The selected item displays architecture, implementation, review, durable cancellation, and reconciliation state.

The webview never opens SQLite directly. It only invokes the shared facade and renders the result.

## Optional chat shortcut

```text
@baldr <task>
@baldr /setup
@baldr /status
@baldr /run <task>
```

Chat-created tasks appear as the same durable items in Baldr Console.

## Installation A UN SOLO clic with automatic bootstrap

Install from Marketplace or from the local `.vsix`. The extension:

1. registers Baldr Router programmatically as MCP;
2. prepares a private, versioned Python runtime from the bundled wheel;
3. verifies version and SHA-256 before reuse;
4. detects Windows, WSL, Remote WSL, Linux, and macOS;
5. uses WSL only when the host cannot run Baldr;
6. preserves an older runtime for rollback;
7. requires VS Code Workspace Trust before provider execution;
8. stores the optional Context7 key in SecretStorage;
9. runs/cache a lifecycle verification during warm-up;
10. exposes only **Baldr: Open** in the Command Palette.

No manual `mcp.json`, global launcher, or `uv tool install` is required. Python 3.11+ must be available in the selected host/WSL environment, and first bootstrap can require network access for wheel dependencies.

## Architecture

```text
Baldr Console
  -> setup / status / run
      -> WorkItemService
          -> durable workflow engine
              -> SQLite / Git worktrees / evidence
```

The extension renders UI and native Quick Picks. Provider routing, profiles, workspace policy, cancellation, recovery, and reconciliation remain in the Python core.

## Packaged runtime

```text
resources/runtime/baldr_router-0.17.0-py3-none-any.whl
runtime/runtime-bootstrap.mjs
runtime/baldr-bootstrap.mjs
```

## Build

From the repository root:

```bash
python scripts/build_release.py
```

Or package only the extension after placing the core wheel under `resources/runtime/`:

```bash
npm ci
npm run check
npm test
npm run package
```
