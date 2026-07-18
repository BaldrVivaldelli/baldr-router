import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";

const artifact = process.env.BALDR_AGENT_ARTIFACT;
assert.ok(artifact, "BALDR_AGENT_ARTIFACT is required");
assert.ok(statSync(artifact).isFile(), "Builder did not expose a regular artifact");
const content = readFileSync(artifact, "utf8");
assert.match(content, /^#!\/usr\/bin\/env node/u);
assert.match(content, /baldr-agent-execution/u);
assert.match(content, /{{OUTPUT_NAME}}/u);
