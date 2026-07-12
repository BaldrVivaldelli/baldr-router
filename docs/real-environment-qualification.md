# Real Environment Qualification — v0.16.1

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
Kiro, WSL, authentication, UI cancellation, or a user's repositories as
qualified.

## Profiles

Mandatory release profiles:

```text
vscode-windows-wsl
vscode-remote-wsl
vscode-linux-native
kiro-windows-wsl
```

Additional supported profiles:

```text
vscode-windows-native
vscode-macos-native
```

The optional profiles can produce qualification receipts, but they do not replace the mandatory Windows/WSL, Remote WSL, Linux, and Kiro gates. Self-hosted GitHub runners used by the qualification workflow must be current enough to execute the Node runtime used by the selected GitHub Actions.

Inspect the frozen definitions:

```bash
baldr-router qualification definitions
```

## 1. Create a qualification workspace

```bash
baldr-router qualification template \
  --profile vscode-windows-wsl \
  --output-dir ./qualification-input
```

This writes:

```text
qualification-input/client-assertions.json
qualification-input/canary-results.json
```

The files contain no secrets. They are operator receipts: complete each item
only after observing the result in the target client.

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

The ten frozen canaries are five bounded code/documentation changes in a Python repository and five in a Node repository. Lifecycle, cancellation, recovery, upgrade, fencing, and secret-redaction behavior are proven separately by the three-pass Lab and the client assertions.

## 4. Run qualification from the exact target environment

```bash
baldr-router trust-workspace /path/to/repository

baldr-router qualification run \
  --profile vscode-windows-wsl \
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

A v0.16.x build may be promoted only when all mandatory profiles have a
`qualified` receipt and the receipts refer to the same release version.
Synthetic CI evidence remains necessary, but never substitutes for this gate.

## Self-hosted CI evaluation

The repository includes a cross-platform entrypoint used by the manual GitHub
workflow:

```bash
uv run --project router python scripts/run_qualification_ci.py \
  --profile vscode-windows-wsl \
  --workspace-root /path/to/repository \
  --evidence-directory ./qualification-input \
  --output-directory ./qualification-output \
  --repeat 3
```

It always exports the redacted receipt before returning a failing status for a
`provisional` or `failed` result. This lets CI upload evidence even when the
qualification gate rejects the candidate.
