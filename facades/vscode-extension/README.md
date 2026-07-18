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

There is no mandatory setup form. Type a task and press Enter; Baldr creates a durable work item and runs it using the active workspace preferences. After it finishes, another plain prompt continues the selected durable conversation instead of creating a disconnected item.

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
/restore
/delete
/setup
/help
```

The commands are aliases over the existing `setup`, `status`, and `run` facade contract.

### `+` menu

Use `+` to create an item, attach the active file or selection, choose a file/folder or active workspace root, switch preset or Git mode, configure Context7, choose role profiles, or open logs.

### Git modes

```text
Git worktree       recommended, isolated, recoverable
Current workspace  in-place, clean Git repository required
Non-Git            explicit consent, reduced recovery guarantees
```

The extension never silently disables Git safety.

## Durable state

Tasks are persisted by the core in SQLite, not in the webview. Closing VS Code or restarting Baldr does not erase the work-item list. The selected item displays architecture, implementation, review, durable cancellation, and reconciliation state.

Each normal follow-up appends an immutable turn to the same work item and
starts a new durable run. Only a bounded structured result from the previous
run is carried forward; private provider transcripts are not. `/resume` is for
recovery, not normal conversation continuity.

The history separates active, completed, and archived sessions. Archive is reversible; archived sessions can be restored or permanently deleted after a confirmation. Permanent deletion removes Baldr's durable session data and never modifies files in the workspace.

The webview never opens SQLite directly. It only invokes the shared facade and renders the result.

## Optional chat shortcut

```text
@baldr <task>
@baldr /setup
@baldr /status
@baldr /run <task>
```

Chat-created tasks appear as the same durable items in Baldr Console.
Further prompts in the same Chat thread reuse the work-item identity. Chat and
Console capture bounded context from explicit references, the active editor,
selection, dirty buffer, and diagnostics. Multi-root workspaces resolve from
those signals or an explicit picker and never silently use folder zero.

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
resources/runtime/baldr_router-0.20.0-py3-none-any.whl
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
