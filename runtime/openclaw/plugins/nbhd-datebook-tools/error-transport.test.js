import assert from "node:assert/strict";
import { after, test } from "node:test";

import register from "./index.js";

const previous = {
  base: process.env.NBHD_API_BASE_URL,
  tenant: process.env.NBHD_TENANT_ID,
  key: process.env.NBHD_INTERNAL_API_KEY,
};
const originalFetch = globalThis.fetch;
process.env.NBHD_API_BASE_URL = "https://datebook.invalid";
process.env.NBHD_TENANT_ID = "tenant-test";
process.env.NBHD_INTERNAL_API_KEY = "internal-test";

after(() => {
  globalThis.fetch = originalFetch;
  for (const [name, value] of [
    ["NBHD_API_BASE_URL", previous.base],
    ["NBHD_TENANT_ID", previous.tenant],
    ["NBHD_INTERNAL_API_KEY", previous.key],
  ]) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
});

test("validation envelopes reach the model without raw non-JSON fallback", async () => {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(definition) { tools.set(definition.name, definition); },
  });
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: "validation_failed",
    message: "title is required",
    details: [{ field: "title", code: "required" }],
  }), { status: 400 });
  const result = await tools.get("nbhd_datebook_read").execute("call-1", {});
  assert.match(result.content[0].text, /validation_failed/);
  assert.match(result.content[0].text, /title is required/);
});

test("non-JSON upstream bodies are replaced with the fixed safe marker", async () => {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(definition) { tools.set(definition.name, definition); },
  });
  globalThis.fetch = async () => new Response("<html>secret proxy bytes</html>", { status: 502 });
  const result = await tools.get("nbhd_datebook_read").execute("call-2", {});
  assert.match(result.content[0].text, /upstream returned a non-JSON response body/);
  assert.doesNotMatch(result.content[0].text, /secret proxy bytes/);
});
