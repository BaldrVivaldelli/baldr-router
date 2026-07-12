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
python scripts/generate_facades.py
python scripts/generate_facades.py --check
```

Do not edit generated contract copies or Agent Plugin command files independently.

## Required checks

```bash
PYTHONPATH=router/src python -m pytest router/tests -q
PYTHONPATH=router/src:facades/kiro/adapter/src \
  python -m pytest facades/kiro/adapter/tests -q
npm --prefix launcher test
npm --prefix facades/vscode-extension test
npm --prefix facades/vscode-extension run check
python scripts/generate_facades.py --check
```

## One development entrypoint

```bash
python scripts/dev.py test
python scripts/dev.py lint
python scripts/dev.py build
python scripts/dev.py verify-release
```

Real-client qualification results must never be fabricated from synthetic CI. Use the templates and attach portable evidence references only.
