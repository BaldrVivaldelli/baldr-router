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
make install-kiro
```

Then install the Power from `facades/kiro/baldr-orchestrator/`.
