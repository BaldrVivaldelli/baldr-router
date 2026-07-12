# Kiro facade

This directory contains the Kiro-only facade over the generic Baldr Router core.

```text
adapter/
  installable Python extension for idempotent workspace hooks

baldr-orchestrator/
  Kiro Power using the shared setup/status/run facade contract

examples/
  generated hook and local MCP examples
```

Install core and adapter together:

```bash
uv tool install --force --editable ./router \
  --with-editable ./facades/kiro/adapter \
  --with-executables-from baldr-kiro-adapter
```

Then install the Power from `facades/kiro/baldr-orchestrator/`.
