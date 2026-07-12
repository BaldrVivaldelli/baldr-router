# Change Log

## 0.17.6

- Fixes Windows Codex discovery and execution when npm exposes `codex.cmd`.
- Makes the cross-platform Codex failure tests independent of POSIX shebangs and executable bits.
- Hardens Windows CI cleanup for read-only Git objects while preserving sensitive-home protections.

## 0.17.5

- Fixes the strict Codex response schema so durable tasks can start with current models.
- Keeps architecture decisions compatible across the exec, app-server, and SDK runners.
- Aligns direct tasks and reviews with the complete structured report contract.

## 0.17.4

- Adds a guided Codex team selector with the models available in the signed-in account and only the supported analysis levels for each model.
- Preserves the current team until the complete selection is confirmed, while keeping saved and advanced configurations available.
- Shows the active model names in the centered team chip, including mixed teams such as Terra and Luna.
- Fixes the bottom option search, including accent-insensitive matches, grouped results, empty feedback, and keyboard navigation.

## 0.17.3

- Rebuilds the composer toolbar with anchored controls, stable SVG icons, responsive labels, and consistent focus states.
- Rewrites the complete console flow in plain Spanish, including native pickers, warnings, progress, and recovery actions.
- Adds in-composer attachment cards with working removal controls.
- Improves the empty state, menu spacing, accessibility, and reduced-motion behavior.

## 0.17.2

- Rewrites the primary Baldr Console wording in plain Spanish for non-technical users.

## 0.17.1

- Opens the `+` menu inside the Baldr Console above the fixed composer, with searchable context and workspace actions.
- Centers composer controls and item actions, and adds a dedicated configuration button.

## 0.17.0

- Adds a dedicated Baldr Activity Bar console as the primary interface.
- Adds durable work items, task list, selected-item timeline, and a fixed composer.
- Adds a `+` menu, slash autocomplete, and clickable Git, preset, role-profile, and Context7 chips.
- Adds visual cancellation and reconciliation without displaying raw workflow JSON.
- Keeps `@baldr` as an optional shortcut that creates the same durable items.
- Keeps one visible Command Palette entry and the frozen `setup`, `status`, `run` facade contract.

## 0.16.1

- Adds a hidden **Qualification** action inside `Baldr: Open` without adding another Command Palette command.
- Detects the exact VS Code target profile: Windows+WSL, Remote WSL, or native Linux.
- Records a redacted client/runtime receipt and runs the three-pass lifecycle lab.
- Creates editable client-assertion and ten-canary evidence templates under extension global storage.
- Evaluates completed evidence into `provisional`, `failed`, or `qualified` receipts with SHA-256.
- Keeps the one-click bootstrap and frozen `setup`, `status`, `run` chat surface.

## 0.15.0

- Bundles Baldr Router's durable SQLite orchestration runtime.
- Displays durable schema, recovery, nonterminal runs, and resolved execution profiles through the shared `status` intent.
- Preserves the same one-click bootstrap and three-intent UX: `setup`, `status`, `run`.
- Keeps native MCP registration, automatic WSL selection, Workspace Trust, Context7 SecretStorage, and lifecycle evidence.
- No new VS Code commands, providers, roles, workflows, or public MCP tools.

## 0.14.0

- Added Validation Lab, workspace probing, deterministic lifecycle verification, and redacted evidence bundles.
