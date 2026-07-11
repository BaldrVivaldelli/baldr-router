# Contributing during the v0.16 durable feature freeze

The current priority is reliability, not surface expansion.

## Accepted changes

- fixes, hardening, tests, packaging, compatibility, performance, and documentation;
- internal refactors that preserve the frozen contract;
- thin client facades under `facades/<client>/`;
- improvements to the one-click VS Code bootstrap that do not alter workflows.

## Not accepted without an explicit freeze-lift decision

- new providers, roles, workflows, or facade intents;
- orchestration logic duplicated in an extension, Power, or Agent Plugin;
- client-specific code imported by the core;
- autonomous recursive delegation.

## Single facade source of truth

Edit `contracts/facade-v1.json`, then run:

```bash
make facades
make facades-check
```

Do not edit generated contract copies or Agent Plugin command files independently.

## Required checks

```bash
make check
```

## One development entrypoint

```bash
make help
make test
make lint
make build
make verify-release
```

Real-client qualification results must never be fabricated from synthetic CI. Use the templates and attach portable evidence references only.
