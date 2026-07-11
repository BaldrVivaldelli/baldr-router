# v0.14 Validation Lab, Workspace Probe, and Evidence

Baldr v0.14 adds three hardening layers without changing the frozen MCP tool, provider, role, or workflow surface.

```text
Baldr Lab
  reproducible, disposable environments and repeated lifecycle runs

Baldr Probe
  safe environment fingerprint + bounded workspace profile

Baldr Verify / Evidence
  deterministic lifecycle self-test + redacted proof bundle
```

## 1. Baldr Lab

The lab runs the same deterministic lifecycle suite repeatedly and requires consecutive passes. The default release threshold is three consecutive successful runs.

```bash
baldr-router lab --mode full --repeat 3
```

The suite validates:

- runtime/install receipt when a managed runtime is present;
- fixture execution and filesystem boundaries;
- ordered progress events;
- cancellation of a process tree;
- MCP start, handshake, stop, and restart;
- transactional update/rollback semantics;
- secret redaction;
- crash and invalid-JSON handling in full mode.

`lab/` contains a Linux container profile, Windows Sandbox template, scripts, and the canonical environment matrix. Windows + WSL and Kiro still require a real Windows VM/machine because a Linux container cannot prove those client boundaries.

## 2. Baldr Probe

### Environment probe

```bash
baldr-router env-report
```

The report records a redacted fingerprint of:

- OS, architecture, Python, WSL state;
- client facade ID/version;
- command availability and versions;
- managed-runtime receipt validity;
- no raw environment export and no secret values.

### Workspace profile

```bash
baldr-router trust-workspace /path/to/repo
baldr-router probe-workspace /path/to/repo
```

Profiling occurs only after workspace trust. It:

- respects Git ignore rules when Git is available;
- excludes secret-shaped files and generated dependency/build directories;
- parses bounded manifest metadata;
- counts file extensions without reading source content;
- discovers package managers, frameworks, scripts, and verification commands;
- caches a fingerprinted profile under the Baldr cache directory.

It does **not** execute workspace scripts or perform an unrestricted source-code crawl.

## 3. Baldr Verify and evidence

```bash
baldr-router verify --mode quick
baldr-router verify --mode full
baldr-router evidence --latest
```

Verification runs against a temporary Git repository and an internal deterministic fixture worker. It does not spend Codex/Kiro credits unless `--include-provider-smoke` is explicitly supplied.

Evidence is stored under:

```text
~/.local/state/baldr-router/evidence/<evidence-id>/
```

Each bundle contains:

```text
environment.json
lifecycle-results.json
workspace-profile.json       # when a trusted workspace is supplied
manifest.json
redaction-report.json
artifact-hashes.json
summary.md
```

Secrets, token-shaped values, raw prompts, source files, and the current home path are excluded/redacted.

## Automatic behavior in client facades

- VS Code prepares the private runtime and runs/caches a quick lifecycle verification during warm-up/status.
- VS Code `/setup` trusts the selected workspace, generates its bounded profile, and reuses current evidence.
- The Kiro adapter runs the same core probe and verification after idempotent workspace onboarding.
- Generic MCP clients receive the same data through the existing `setup` and `status` intents and `router_doctor`; no new MCP tools were added.

## Release acceptance

A candidate is considered repeatable only when the required profile passes three consecutive clean runs. Real-client results belong in `e2e/REAL_ENVIRONMENT_MATRIX.md`; synthetic tests are not marked as real E2E passes.
