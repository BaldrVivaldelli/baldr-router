# Change Log

## 0.19.0

- Replaces internal phase statuses with an understandable **Ahora** card, a three-stage narrative, and a final result that explains what changed, what was checked, and what remains.
- Makes Planning, Execution, and Review expandable, groups correction rounds, preserves expansion across reloads, and keeps providers, models, IDs, commands, and traces under technical details.
- Shows only allowlisted, evidence-backed live activity; it never renders raw prompts, reasoning, provider streams, secrets, or private paths.
- Adds safe, paginated phase deliverables for every Planning, Execution, and Review round, including on-demand access to retries beyond the compact status window.
- Adds clear attention cards and contextual actions for review findings, interruption, conflict, recovery, cancellation, and failure.
- Improves keyboard navigation, a focus-contained deliverable viewer, screen-reader announcements, reduced motion, zoom and narrow-sidebar layouts, and compatibility with work items created before this release.
- Uses a compact status path plus adaptive visible-only polling to reduce repeated runtime work and output-channel noise.
- Adds a simplified external-agent selector with explicit per-stage identities, readiness, version, digest and permission information instead of ambiguous provider/model choices.
- Adds external-agent administration and deterministic automatic/pinned/configured team modes backed by the shared Baldr catalog used by CLI and Kiro/MCP.
- Shows a clickable changed-files summary with added/modified/deleted counts and direct navigation to the affected workspace file.
- Allows a trusted, write-capable external agent to continue directly from its immutable manifest instead of showing a redundant write-authorization interruption.

## 0.18.0

- Makes **Automatic protection** the recommended default: exact Git roots use the existing worktree flow, while non-Git folders and selected repository subdirectories use a durable BALDR-managed shadow workspace.
- Runs providers only in the protected copy, with a private helper Git repository and content-addressed manifests as the portable recovery authority.
- Adds hash preflight, durable per-operation publication cursors, idempotent retry, conflict evidence, and safe inspect/continue/apply/discard actions for shadow workspaces.
- Excludes VCS metadata, configured secrets, and generated artifacts; adds visible file, byte, depth, symlink, and portable-path limits.
- Uses shadows for dirty/unborn Git roots, blocks providers without an enforced workspace boundary, and revalidates every publication operation after durable intent.
- Keeps inspect/continue/apply/discard available after failed phases or requested review changes, and never expands a direct-mode subfolder to its Git parent.
- Preserves legacy worktree/direct/unprotected task semantics, adds configurable shadow cleanup and retention, and limits recovery choices to actions proven safe for the recorded state.
- Clarifies the protection selector and recovery wording for non-technical users.

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
