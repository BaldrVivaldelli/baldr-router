import assert from "node:assert/strict";
import test from "node:test";

import { descriptor } from "../dist/driver.js";

test("driver describes an exact TypeScript identity over JSONL", () => {
  const value = descriptor();

  assert.equal(value.id, "baldr.typescript");
  assert.equal(value.language, "typescript");
  assert.match(value.digest, /^sha256:[0-9a-f]{64}$/);
  assert.deepEqual(value.operations, ["test", "build"]);
});
