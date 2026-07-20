# Real Environment Qualification — v0.20

Baldr distinguishes three different claims:

```text
synthetic validation
  deterministic fixtures, Lab, Probe, Verify, packaging tests

provisional qualification
  the target environment ran the three-pass lab, but real-client assertions
  or real-repository canaries are still incomplete

qualified
  the exact client/runtime profile passed every mandatory assertion and ten
  evidenced canary tasks across two distinct real repositories
```

A build machine can produce synthetic evidence, but it cannot mark VS Code,
WSL, Codex authentication, UI cancellation, or a user's repositories as
qualified. Kiro remains independently qualifiable, but it is outside the
v0.20 promotion gate for this iteration.

## Profiles

Mandatory v0.20 promotion profile:

```text
vscode-remote-wsl
```

Supported profiles that are deferred from this promotion gate:

```text
vscode-windows-wsl
vscode-linux-native
vscode-windows-native
vscode-macos-native
kiro-windows-wsl
```

The deferred profiles keep their implementation, packaging, tests, runbooks,
and ability to produce qualification receipts. They neither block nor replace
the mandatory VS Code Remote WSL + Codex receipt. Kiro real-client
qualification will be completed in a later iteration. Self-hosted GitHub
runners used by the qualification workflow must be current enough to execute
the Node runtime used by the selected GitHub Actions.

Inspect the frozen definitions:

```bash
baldr-router qualification definitions
```

## 1. Create a qualification workspace

```bash
baldr-router qualification template \
  --profile vscode-remote-wsl \
  --output-dir ./qualification-input
```

This writes:

```text
qualification-input/client-assertions.json
qualification-input/canary-results.json
```

The files contain no secrets. They are operator receipts: complete each item
only after observing the result in the target client.

The qualification runner fills only evidence it can prove in the same real
run. The three-pass Lab automatically records installation integrity, MCP
restart, ordered progress, process-tree cancellation, transactional rollback,
secret redaction and a disposable local SQLite durability contract. That
contract reopens the database, recovers interrupted read and write phases,
rejects a stale lease and conflicting idempotency key, isolates provider
sessions, runs maintenance and resolves the configured planning,
implementation and review profiles. Each automatic assertion references the
Lab evidence ID and all three run IDs.

The Lab never auto-attests Workspace Trust blocking, uninstall/reinstall, UI
behavior, accessibility, file/diff navigation or repository canaries. Those
remain `pending` until they are observed in the exact VS Code client or real
repository named by the receipt.

## 2. Complete the client assertions

Each required assertion has one of these states:

```text
pending
passed
failed
```

A passing item should contain a portable evidence reference, such as:

```json
{
  "id": "vscode.cancel_from_ui",
  "status": "passed",
  "evidence": ["screen-recording:cancel-2026-07-11", "evidence:br-lifecycle-..."],
  "notes": "Cancellation reached durable cancelled and no child process remained."
}
```

Do not paste API keys, prompts, source code, usernames, or absolute paths.

## 3. Execute the ten canaries

Use two different real Git repositories and record five tasks in each. Every
passing task requires:

```text
run_id
evidence_id
orphan_processes = 0
test/verification references
```

`evidence_id` is not a free-form note. Use the `br-workflow-...` identifier
returned in the technical result of that exact run. Qualification verifies the
bundle on local disk, every artifact hash, its canonical durable-state
fingerprint, privacy projection, Baldr version and matching `run_id`. Reusing
one run or evidence bundle for multiple canaries is rejected. The run status
must also match the canary: for example, `cancel-during-implementation`
requires durable `cancelled`, while `publication-conflict` may be
`awaiting_reconciliation` before its explicit resolution.

Use a real Python repository for `repository-a` and a different real Node
repository for `repository-b`. Work from disposable clones or a restorable
snapshot because several canaries deliberately interrupt execution or create a
conflict. The frozen tasks are:

| Repository | Canary | Observation required |
| --- | --- | --- |
| Python | `normal-change` | A bounded Codex change reaches approved with a valid structured report. |
| Python | `tested-change` | Codex changes code, runs relevant existing tests and reports commands that can be reproduced. |
| Python | `cancel-during-implementation` | Cancellation is initiated while Codex is writing; the run reaches durable cancelled and has zero orphan processes. |
| Python | `recover-read-only-step` | The client/runtime is interrupted during a read-only phase and continues without duplicating completed effects. |
| Python | `reconcile-write-unknown` | A write interruption becomes `unknown` and is resolved through an explicit action, never a blind retry. |
| Node | `publication-conflict` | A concurrent Git change produces a durable conflict or safe reconciliation instead of overwriting the original. |
| Node | `upgrade-preserves-state` | The packaged runtime is upgraded and the existing configuration, SQLite run and rollback receipt remain usable. |
| Node | `session-reuse` | A compatible Codex session is reused and a deliberately incompatible identity is not. |
| Node | `lease-fencing` | A second worker takes over an expired lease and the stale epoch cannot persist a result. |
| Node | `secret-redaction` | A synthetic marker shaped like a secret is absent from logs, telemetry and exported workflow evidence. |

The three-pass Lab proves the underlying lifecycle contracts independently;
the canaries prove that those same boundaries are operable in the exact VS
Code + Codex profile and real repositories being promoted. A Lab scenario is
therefore supporting evidence, not a substitute for the canary run ID.

## 4. Run qualification from the exact target environment

From VS Code Remote WSL, the recommended path is **Baldr → + → Calificar VS
Code + Codex**. It creates the evidence templates in extension global storage,
runs the same command below with a real Codex provider smoke, renders the
result, and offers to open either pending evidence file.

The equivalent operator command is:

```bash
baldr-router trust-workspace /path/to/repository

baldr-router qualification run \
  --profile vscode-remote-wsl \
  --workspace-root /path/to/repository \
  --client-assertions ./qualification-input/client-assertions.json \
  --canary-results ./qualification-input/canary-results.json \
  --repeat 3
```

The result is `qualified` only when all gates pass. Missing real evidence
produces `provisional`; explicit failed assertions or lifecycle failures produce
`failed`.

## 5. Inspect the receipt

```bash
baldr-router qualification status --latest
```

Receipts live outside the repository:

```text
~/.local/state/baldr-router/qualification/<qualification-id>/
```

Each bundle includes:

```text
receipt.json
summary.md
environment.json
workspace-profile.json
lab-result.json
client-assertions.json
canary-results.json
requirements.json
artifact-hashes.json
```

The receipt uses a canonical SHA-256 digest and excludes raw prompts, source
code, secrets, full home paths, and raw workspace paths.

## Promotion rule

A v0.20.x build may be promoted only with a `qualified` receipt for
`vscode-remote-wsl`, for the same release version, whose provider smoke proves
Codex. Synthetic CI evidence remains necessary, but never substitutes for
this gate. A Kiro receipt is useful independent evidence, but cannot satisfy
or block this iteration's promotion policy.

Verify the release input explicitly:

```bash
baldr-router qualification promotion-status \
  --receipt ./qualification-output \
  --release-version 0.20.0
```

The release workflow is manual and must be dispatched from the `v0.20.0` tag
with the run id of the successful `vscode-remote-wsl` qualification workflow.

## Self-hosted CI evaluation

The repository includes a cross-platform entrypoint used by the manual GitHub
workflow:

```bash
uv run --project router python scripts/run_qualification_ci.py \
  --profile vscode-remote-wsl \
  --workspace-root /path/to/repository \
  --evidence-directory ./qualification-input \
  --output-directory ./qualification-output \
  --repeat 3
```

It always exports the redacted receipt before returning a failing status for a
`provisional` or `failed` result. This lets CI upload evidence even when the
qualification gate rejects the candidate.
