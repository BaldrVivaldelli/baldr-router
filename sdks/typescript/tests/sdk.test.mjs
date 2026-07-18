import assert from "node:assert/strict";
import test from "node:test";

import {
  Agent,
  ContractError,
  canonicalDigest,
  validateRef,
} from "../dist/index.js";

test("canonical digest is independent of object key order", () => {
  assert.equal(canonicalDigest({ b: 2, a: 1 }), canonicalDigest({ a: 1, b: 2 }));
});

test("Agent requires immutable identity and write capability", () => {
  assert.throws(() => validateRef("local://example/worker@latest"), ContractError);
  assert.throws(
    () =>
      new Agent({
        ref: "local://example/worker@1.0.0",
        owner: "team",
        capabilities: ["workspace.read"],
        effectMode: "workspace-write",
      }),
    ContractError,
  );
});

test("manifest identity is deterministic", () => {
  const agent = new Agent({
    ref: "local://example/worker@1.0.0",
    owner: "team",
    capabilities: ["workspace.read"],
  });
  const first = agent.manifest("http-json", { url: "https://example.invalid/invoke" });
  const second = agent.manifest("http-json", { url: "https://example.invalid/invoke" });
  assert.equal(first.digest, second.digest);
});
