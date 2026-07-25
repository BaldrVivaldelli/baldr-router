# Contributing during the v0.20 durable feature freeze

The current priority is reliability, not surface expansion. `FEATURE_FREEZE.md`
is the authoritative statement of the frozen surface, and
`baldr_router.release_policy` is its machine-readable form.

## Accepted changes

- fixes, hardening, tests, packaging, compatibility, performance, and documentation;
- internal refactors that preserve the frozen contract;
- thin client facades under `facades/<client>/`;
- improvements to the one-click VS Code bootstrap that do not alter workflows;
- external agent platform work (SDKs, Agent Builder, Agent Runner, Agent Manager)
  that does not add public facade intents.

## Not accepted without an explicit freeze-lift decision

- new providers, roles, workflows, or facade intents;
- orchestration logic duplicated in an extension, Power, or Agent Plugin;
- client-specific code imported by the core;
- autonomous recursive delegation.

## Single facade source of truth

Edit `contracts/facade-v1.json`, then run:

```bash
python scripts/generate_facades.py
python scripts/generate_facades.py --check
```

Do not edit generated contract copies or Agent Plugin command files independently.

## One development entrypoint

`scripts/dev.py` is the supported way to run everything, on every platform. It
covers all five Python packages and all Node workspaces, so prefer it over
per-suite commands:

```bash
python scripts/dev.py test
python scripts/dev.py lint
python scripts/dev.py typecheck
python scripts/dev.py coverage
python scripts/dev.py audit
python scripts/dev.py build
python scripts/dev.py verify-release
```

`test` and `lint` are the two required gates; CI runs the same commands.
`typecheck`, `coverage`, and `audit` are advisory today and are expected to
become required as their baselines tighten.

## Running a single suite

Use these only to iterate on one component. They are a subset of
`python scripts/dev.py test`, not a replacement for it:

```bash
uv run --project router --extra dev pytest -q
uv run --project facades/kiro/adapter --extra dev pytest -q
uv run --project sdks/python --extra dev pytest -q
uv run --project tooling/agent-builder --extra dev pytest -q
uv run --project runtimes/agent-runner --extra dev pytest -q
npm --prefix launcher test
npm --prefix facades/vscode-extension test
npm run test:agents
```

## Release versions

The release version has a single source of truth in the repository and is
verified across all manifests:

```bash
python scripts/check_release_consistency.py
python scripts/check_release_consistency.py --print-version
```

Never hardcode a version into a workflow or script; read it from
`--print-version` instead.

## Real-client qualification

Real-client qualification results must never be fabricated from synthetic CI.
Use the templates and attach portable evidence references only. To see exactly
what a provisional receipt still needs before promotion, including the client
assertions that remain pending by name:

```bash
uv run --project router baldr-router qualification promotion-status --receipt <dir>
```

The `blocking` section of that output, not a hand-written count in a document,
is the source of truth for remaining qualification work.
