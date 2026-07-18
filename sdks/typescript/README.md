# Baldr Agent SDK for TypeScript

`@baldr/agent-sdk` is the authoring/runtime API for TypeScript agents owned
outside Baldr. It implements the same `agent-execution-v1` identity, event and
result semantics as the Python SDK. It does not include Builder, publication or
Router code.

```bash
npm install @baldr/agent-sdk
```

Antes de su primera publicación en el registry, el mismo paquete puede
instalarse desde el artefacto de release:

```bash
npm install ./baldr-agent-sdk-0.19.0.tgz
```

```ts
import { Agent } from "@baldr/agent-sdk";

const agent = new Agent({
  ref: process.env.BALDR_AGENT_REF!,
  owner: "product-team",
  capabilities: ["workspace.read", "role.reviewer"],
});

agent.invoke((_request, context) => {
  context.emit("verifying", "Reviewing the workspace.");
  return { ok: true };
});

process.exitCode = await agent.serveStdio();
```

The runtime reads one bounded JSONL request, validates exact `AgentRef +
digest`, checks capabilities and effect mode, emits structured events and
returns a terminal result. `localProcessManifest` and `writeManifest` create
the same canonical manifest shape as the Python SDK.

For a complete generated repository, use the separately distributed Builder:

```bash
baldr-agent init ./my-agent --name my-agent --owner my-team \
  --namespace product --language typescript
```
