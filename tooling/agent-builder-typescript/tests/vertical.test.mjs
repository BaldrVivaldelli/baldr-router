import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { handle } from "../dist/driver.js";

function sha256(value) {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "baldr-typescript-driver-"));
  mkdirSync(join(root, "src"));
  mkdirSync(join(root, "tests"));
  const files = new Map([
    ["baldr-agent.toml", "schema_version = 2\n"],
    [
      "src/agent.ts",
      `import { Agent } from "@baldr/agent-sdk";
export async function main(): Promise<number> {
  const agent = new Agent({
    ref: process.env.BALDR_AGENT_REF ?? "local://driver/fixture@1.0.0",
    owner: "driver-test",
    capabilities: ["workspace.read", "role.architect"],
  });
  agent.invoke(() => ({ ok: true }));
  return agent.serveStdio();
}
`,
    ],
    [
      "tests/agent.test.mjs",
      `import assert from "node:assert/strict";
import { statSync } from "node:fs";
assert.ok(statSync(process.env.BALDR_AGENT_ARTIFACT).isFile());
`,
    ],
  ]);
  for (const [name, content] of files) {
    writeFileSync(join(root, name), content, "utf8");
  }
  const inventory = [...files]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name]) => {
      const content = readFileSync(join(root, name));
      return [name, sha256(content), content.length];
    });
  return { root, digest: sha256(JSON.stringify(inventory)) };
}

function request(kind, root, digest, outputRoot = null) {
  return {
    contract: "baldr-builder-driver",
    version: 1,
    kind,
    request_id: `${kind}-fixture`,
    source_root: root,
    source_digest: digest,
    source_paths: ["baldr-agent.toml", "src", "tests"],
    project_name: "typescript-fixture",
    project_version: "1.0.0",
    entrypoint: "src/agent.ts",
    test_command: ["{node}", "tests/agent.test.mjs"],
    timeout_seconds: 30,
    target: "agent-execution-v1",
    network: "inherit",
    reproducible: true,
    output_root: outputRoot,
  };
}

test("driver tests and reproducibly builds a TypeScript agent", async () => {
  const { root, digest } = fixture();
  try {
    const tested = await handle(request("test-request", root, digest));
    assert.equal(tested.status, "succeeded");
    assert.equal(tested.tests.status, "passed");

    const first = await handle(
      request("build-request", root, digest, join(root, "dist-one")),
    );
    const second = await handle(
      request("build-request", root, digest, join(root, "dist-two")),
    );
    assert.equal(first.status, "succeeded");
    assert.equal(first.artifact.digest, second.artifact.digest);
    assert.deepEqual(
      readFileSync(first.artifact.path),
      readFileSync(second.artifact.path),
    );
    assert.ok(existsSync(first.artifact.path));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
