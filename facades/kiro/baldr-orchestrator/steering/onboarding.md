# Kiro onboarding facade

This file contains only Kiro-specific onboarding. The shared setup behavior comes from the Baldr `setup` intent.

## Flow

1. Use the shared `setup` prompt and call `router_doctor`.
2. Call `router_extension_status` and verify that `baldr-kiro-adapter` loaded.
3. If missing, install core and adapter together:

```bash
uv tool install --force --editable ./router \
  --with-editable ./facades/kiro/adapter \
  --with-executables-from baldr-kiro-adapter
```

4. Call `kiro_workspace_status`.
5. Call `kiro_install_workspace`. It is idempotent and returns the core workspace profile plus cached lifecycle verification.
6. For `skipped_*` results, explain the conflict and let the user choose; never force automatically.
7. Ask the Context7 decision from the shared setup plan:

```text
1. Not now.
2. Store a key locally outside chat.
3. Use an existing CONTEXT7_API_KEY.
4. Show instructions for obtaining a key.
```

8. Confirm the managed hook state is `managed_clean`.

## Hook ownership

The adapter may create or update only its own generated file:

```text
.kiro/hooks/baldr-router.generated.kiro.hook
```

It must not overwrite user-owned or manually modified hooks without explicit force approval. Backups remain enabled by default for managed upgrades.

## Probe and evidence

The Kiro facade does not scan the workspace itself. Use the profile returned by the core/adapter only after trust. The setup/status response includes the latest lifecycle evidence. Three consecutive passes in a real environment are required before marking that environment validated.
