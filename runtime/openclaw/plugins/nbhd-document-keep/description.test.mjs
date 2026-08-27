import { test } from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

function collectTools() {
  const tools = {};
  register({
    pluginConfig: { documentIngestionEnabled: true },
    registerTool(def) { tools[def.name] = def; },
  });
  return tools;
}

test("document keep description carries the whole-source destination rule", () => {
  const description = collectTools().nbhd_document_keep.description;
  assert.ok(description.includes("A whole-source keep goes into ONE dedicated verbatim note"));
  assert.ok(description.includes("never today's daily note"));
});
