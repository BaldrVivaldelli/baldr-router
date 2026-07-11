# v0.13 Release Candidate Hardening

Baldr Router v0.13 keeps the v0.12 feature surface frozen. This release changes reliability, packaging, safety, diagnostics, and test coverage only. It does **not** add providers, roles, workflows, or public MCP tools.

## Hardening matrix

| # | Area | Implementation | Automated validation | Real-environment validation |
|---|---|---|---|---|
| 1 | VS Code on Windows with Baldr in WSL | Shared bootstrap selects the Windows host first and transparently bridges to an existing or managed WSL runtime. | Launcher WSL fallback tests and shared-bootstrap identity test. | `e2e/vscode-windows-wsl.md` |
| 2 | VS Code Remote WSL | The extension resolves the router directly in the remote Linux extension host and passes workspace trust roots. | Linux/Remote-WSL target resolution test. | `e2e/vscode-remote-wsl.md` |
| 3 | Kiro Power | The optional Kiro adapter persistently trusts the selected Git workspace and installs managed hooks idempotently. | Adapter onboarding E2E test, extension discovery/packaging test. | `e2e/kiro-power.md` |
| 4 | Trusted workspaces | Providers are blocked unless the path is trusted, exists, is a directory, is a Git repository by default, and is outside sensitive roots. | `test_workspace_policy.py` plus workflow/task conformance tests. | All real E2E runbooks verify the active workspace report. |
| 5 | Cancellation and process cleanup | Provider subprocesses run in process groups. Timeout, cancellation, shutdown, and extension cancellation terminate descendants. | Python and Node process-tree tests. | Runbooks include cancel/restart checks. |
| 6 | Runtime upgrades | VS Code/launcher runtimes are versioned, wheel-hash verified, installed transactionally with rollback, and retain only a bounded number of versions. | Manifest/hash and pruning tests; packaged-wheel bootstrap validation. | Clean-machine and upgrade checklist. |
| 7 | Codex failures | Missing binary/login, timeout, signal abort, nonzero exit, malformed JSONL, and invalid structured output have stable error codes. | Fake-Codex integration tests. | Runbooks include missing-login and cancellation scenarios. |
| 8 | Facade conformance | CLI, Python facade, MCP prompts/tools, VS Code, Kiro, Agent Plugin, and generic MCP all consume the frozen `setup/status/run` contract. | Contract generation check and CLI ↔ MCP semantic conformance test. | Manual chat smoke checks use the same intents. |
| 9 | Context7 secrets | VS Code stores the key in `SecretStorage`; the runtime receives it only through process environment. Python/TypeScript redaction protects output, telemetry, cache, and diagnostics. | Python and Node secret-leak regression tests. | Runbooks verify that logs/config do not contain the key. |
| 10 | Clean-machine installation | The VSIX carries the core wheel and bootstraps a private versioned runtime without manual MCP JSON. | Wheel/VSIX/package integrity and isolated-install validation. | `docs/clean-machine-install.md` |

## Frozen surface

The following remain unchanged for the v0.13 line:

- providers: `codex`, `kiro-cli`
- roles: `architect`, `implementer`, `reviewer`
- workflow: `architect-implement-review`
- facade intents: `setup`, `status`, `run`
- core MCP tool and prompt lists in `baldr_router.release_policy`

`router/tests/test_frozen_surface.py` fails if the public core surface changes accidentally.

## Error codes

Provider and workspace failures are machine-readable. Important v0.13 codes include:

- `workspace_not_trusted`
- `workspace_git_required`
- `workspace_sensitive_path_blocked`
- `codex_not_found`
- `codex_not_authenticated`
- `codex_timeout`
- `codex_process_aborted`
- `codex_process_failed`
- `codex_invalid_structured_output`
- `router_reentry_blocked`
- `router_max_depth_exceeded`
- `same_provider_recursion_blocked`

Clients should display `error.message`, preserve `error.code`, and only retry automatically when `error.retryable` is true.

## Release-candidate exit criterion

Automated synthetic validation is necessary but not sufficient. A release candidate should be promoted only after the three real-environment runbooks pass and at least ten representative tasks complete across two real Git repositories without technical configuration edits, leaked secrets, or orphaned provider processes.
