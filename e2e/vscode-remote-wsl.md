# E2E: VS Code Remote WSL + Codex

This is the only mandatory real-environment profile for v0.20 promotion. Kiro
and the other VS Code profiles remain supported, but do not block this
iteration.

## Prepare the real client

1. Open a real disposable Git repository through **WSL: Open Folder in WSL**.
2. Install `baldr-router-vscode-0.20.0.vsix` in the WSL extension host and
   reload the window.
3. Grant **Workspace Trust**. Do not edit JSON, TOML, or MCP configuration.
4. Confirm the Baldr output reports `version: 0.20.0`, `kind: host`,
   `source: installed-private-host-runtime`, and never invokes `wsl.exe`.
5. Open Baldr and complete one bounded task with Codex. Planning, execution,
   and review must finish; implementation must write directly without a
   per-task authorization pause.

## Observe the product assertions

Use only evidence observed in this exact client. Record screenshots, screen
recordings, run ids, evidence ids, or redacted log references; never paste
prompts, source, usernames, secrets, or absolute workspace paths.

- verify ordered narrative progress and understandable stage summaries;
- inspect the final added/modified/deleted file groups and open a changed file;
- cancel a long implementation from the UI and confirm no Codex child remains;
- reload the VS Code window and confirm progress and the durable session return;
- continue the recovered conversation;
- exercise a safe read-only retry;
- interrupt a write-capable attempt and confirm Baldr requests explicit
  reconciliation instead of retrying blindly;
- confirm technical details do not expose prompts, reasoning, commands, paths,
  source content, or secrets;
- navigate the console by keyboard, verify focus and readable forced/high
  contrast states;
- open one legacy durable item and confirm it degrades safely.

### Capture the client-only assertions without ambiguity

Use a disposable repository for destructive observations and keep each
observation tied to this VS Code Remote WSL session:

| Assertion | Real-client action | Minimum evidence |
| --- | --- | --- |
| `workspace.untrusted_blocked` | Open the disposable repository without trusting it and try to start Baldr. Trust it only after the blocked state is visible. | Screenshot of the blocked action and the client session identifier. |
| `vscode.mcp_visible` | Run **MCP: List Servers**, open Baldr and confirm that the server is running and exposes its tools. | Screenshot or redacted VS Code MCP log reference. |
| `vscode.cancel_from_ui` | Start a deliberately long implementation, press **Cancelar** in Baldr and wait for durable `cancelled`. | Screenshot plus run/evidence IDs and a process inspection showing zero children. |
| `vscode.changed_file_navigation` | In a completed real task, select an added or modified path from the file-change card. | Screenshot showing the file opened in the same trusted workspace, tied to the work-item/run ID. |
| `vscode.correction_rounds_grouped` | Open a task whose reviewer requested a correction and expand its execution/review stage. | Screenshot showing each attempt under its corresponding round. |
| `vscode.attention_action_clear` | Open a prepared attention or unknown-write item. | Screenshot showing one explicit primary next action instead of a generic failure. |
| `vscode.progress_accessible` | Reach all interactive controls with Tab/Shift+Tab, activate them with Enter/Space and inspect a forced/high-contrast theme. | Operator note or recording covering focus order, visible focus and readable states. |
| `vscode.polling_quiet` | Observe an active task until polling reaches its stable/idle interval, then hide the view. | Timestamped process/log observation proving single-flight polling, backoff and stop-on-hide. |

Do not mark an assertion from unit tests alone. Automated coverage can support
the observation, but the table above requires behavior from this exact client.

`reconciliation.all_actions` is not a client-only observation. The real
three-pass Lab exercises every allowed action against an independent
disposable repository, SQLite database and durable run. Qualification attests
it automatically only when all actions were offered by their originating
state and produced the expected durable event and outcome in all three passes.

## Run qualification from Baldr

Open **Baldr → + → Calificar VS Code + Codex**. The action:

1. records the real VS Code client receipt;
2. selects `vscode-remote-wsl` automatically;
3. runs the full lifecycle matrix three times with a real Codex provider smoke;
4. creates the assertions and canary files in VS Code global storage;
5. opens the qualification report;
6. offers **Abrir assertions** and **Abrir canarios** while evidence remains
   incomplete.

Mark an item `passed` only after observing it. Every passing assertion needs at
least one portable evidence reference.

## Complete the canaries

Use two distinct real Git repositories: one Python-oriented and one
Node/TypeScript-oriented. Complete the five frozen task ids assigned to each
repository. Every task requires:

- the exact durable `run_id`;
- its `evidence_id`;
- relevant test or verification references;
- `orphan_processes: 0`;
- all six frozen invariants set to `true` only after verification.

Run **Calificar VS Code + Codex** again after saving the evidence files. Pass
only when the result is `qualified`, the report shows 35/35 assertions, 10/10
canaries over 2/2 repositories, the provider is Codex, and
`qualification promotion-status` accepts the resulting receipt.
