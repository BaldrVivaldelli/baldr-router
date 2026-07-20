# E2E: Kiro Power and optional adapter

1. Install the v0.20 core and `baldr-kiro-adapter` in the same Python environment.
2. Install `baldr-router-launcher-0.20.0.tgz` on the host that starts Kiro; run `baldr-router-launcher detect` and verify it resolves Router 0.20.0 on the host or WSL.
3. Install the packaged `baldr-orchestrator-kiro-0.20.0.zip` through Kiro's
   **Add Custom Power → Import power from a folder → Install** flow. Verify its
   `mcp.json` uses `baldr-router-launcher mcp` and Kiro generated the
   namespaced entry under `powers.mcpServers`. Copying files into the installed
   directory is not an installation and does not satisfy this assertion.
4. Restart/reconnect Kiro's MCP server if the host process had an old PATH.
   Fail the profile as `environment_unavailable` when **Kiro - MCP Logs** says
   `excluded by registry-only access mode`; the enterprise MCP Registry policy
   must be changed by its administrator and must never be bypassed by Baldr.
5. Ask for the shared setup intent. Verify `router_extension_status` reports adapter `kiro`.
6. Confirm setup asks whether Context7 should be enabled, without asking for a key in chat.
7. Approve workspace setup. Verify `.kiro/hooks/baldr-router.generated.kiro.hook` appears and the workspace is in Baldr's trust list.
8. Repeat onboarding. Verify the hook action is `unchanged` rather than duplicated.
9. Execute a harmless Kiro spec task and confirm the generated `PreTaskExec` hook routes through the shared Baldr `run` intent.
10. Cancel a long provider run and verify no child processes remain.
11. Manually modify the generated hook, rerun setup, and verify Baldr refuses to overwrite it without `force`.

Pass when the core remains client-agnostic, the adapter owns all Kiro hooks, and onboarding is safe/idempotent.
