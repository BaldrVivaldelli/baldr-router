# Baldr Agent SDK for Python

This package lets another repository declare and run an externally owned
agent without importing `baldr_router` internals. Agent code remains in the
owning team's repository; Baldr receives a versioned manifest and communicates
through `baldr-agent-execution` v1.

```python
from baldr_agent_sdk import Agent

agent = Agent(
    ref="company://product/reviewer@1.0.0",
    owner="product-team",
    capabilities=("workspace.read", "role.reviewer"),
)

@agent.invoke
def review(request, context):
    context.emit("verifying", "Reviewing the requested change.")
    return {"ok": True, "final_report": {"status": "approved"}}

if __name__ == "__main__":
    agent.serve_stdio()
```

This distribution contains only the Python authoring/runtime API. It does not
build, install, publish or roll back releases. Use the separately distributed
[`Baldr Agent Builder`](../../tooling/agent-builder/README.md) for the
`baldr-agent` lifecycle CLI and `baldr-agent.toml` project contract.

Call `agent.local_process_manifest(...)` directly only when another release
system owns installation and publication.

Full runtime, installation and security guidance lives in
[`docs/external-agent-runtime.md`](../../docs/external-agent-runtime.md).
