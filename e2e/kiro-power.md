# E2E: Kiro Power and optional adapter

1. Install the v0.20 core and `baldr-kiro-adapter` in the same Python environment.
2. Install `facades/kiro/baldr-orchestrator/` as the Power.
3. Restart/reconnect Kiro's MCP server if the host process had an old PATH.
4. Ask for the shared setup intent. Verify `router_extension_status` reports adapter `kiro`.
5. Confirm setup asks whether Context7 should be enabled, without asking for a key in chat.
6. Approve workspace setup. Verify `.kiro/hooks/baldr-router.generated.kiro.hook` appears and the workspace is in Baldr's trust list.
7. Repeat onboarding. Verify the hook action is `unchanged` rather than duplicated.
8. Execute a harmless Kiro spec task and confirm the generated `PreTaskExec` hook routes through the shared Baldr `run` intent.
9. Cancel a long provider run and verify no child processes remain.
10. Manually modify the generated hook, rerun setup, and verify Baldr refuses to overwrite it without `force`.

Pass when the core remains client-agnostic, the adapter owns all Kiro hooks, and onboarding is safe/idempotent.
