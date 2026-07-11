# Suggested VS Code agent instructions for generic MCP use

Use the shared Baldr Router facade contract rather than calling providers directly.

- `setup`: inspect runtime readiness and optional Context7 onboarding.
- `status`: return compact health, role/provider assignments, and recent runs.
- `run`: execute the configured architect → implementer → reviewer workflow for the active workspace and task.

Do not duplicate routing or verification logic in VS Code instructions. Treat Baldr's `final_report` as the consolidated provider output and still inspect the resulting diff and verification evidence before declaring the work complete.
