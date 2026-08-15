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

test("create timeout is an honest error and a logical retry keeps its request id", async () => {
  const tools = new Map();
  const toolContext = {
    messageChannel: "ios",
    sessionKey: "agent:main:openai-user:thread:00000000-0000-4000-8000-000000000099",
  };
  register({
    pluginConfig: {},
    registerTool(definition) {
      const tool = typeof definition === "function" ? definition(toolContext) : definition;
      tools.set(tool.name, tool);
    },
  });
  const requestBodies = [];
  globalThis.fetch = async (_url, options) => {
    requestBodies.push(JSON.parse(options.body));
    const error = new Error("mock abort");
    error.name = "AbortError";
    throw error;
  };
  const params = {
    items: [{ title: "Buy milk" }],
    direct_user_originated: true,
  };
  const tool = tools.get("nbhd_datebook_add_apple_reminder");

  for (const callId of ["first-call", "model-retry-call"]) {
    await assert.rejects(
      tool.execute(callId, params),
      (error) => {
        assert.equal(error.code, "request_still_processing");
        assert.match(error.message, /Nothing was created in Apple Calendar or Reminders yet/);
        assert.match(error.message, /did not confirm whether an approval request was recorded/);
        assert.match(error.message, /DO NOT re-call this tool automatically/);
        assert.match(error.message, /do not promise that an approval will appear shortly/);
        assert.doesNotMatch(error.message, /the approval will appear shortly/);
        return true;
      },
    );
  }

  assert.equal(requestBodies.length, 2);
  assert.equal(requestBodies[0].request_id, "first-call");
  assert.equal(requestBodies[1].request_id, "first-call");
});

test("bounded server failure says nothing was created and is retriable", async () => {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(definition) {
      const tool = typeof definition === "function" ? definition({ messageChannel: "ios" }) : definition;
      tools.set(tool.name, tool);
    },
  });
  globalThis.fetch = async () => new Response(JSON.stringify({
    state: "request_temporarily_unavailable",
    retriable: true,
    created: false,
    guidance: "Nothing was created yet. Calendar & Reminders is temporarily busy; retry this request later.",
  }), { status: 503 });

  const result = await tools.get("nbhd_datebook_add_apple_reminder").execute("busy-call", {
    items: [{ title: "Buy milk" }],
    direct_user_originated: true,
  });

  assert.equal(
    result.content[0].text,
    "Nothing was created yet. Calendar & Reminders is temporarily busy; retry this request later.",
  );
  assert.equal(result.details.retriable, true);
  assert.equal(result.details.created, false);
});
