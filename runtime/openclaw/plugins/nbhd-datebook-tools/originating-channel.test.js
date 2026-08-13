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

function toolsForChannel(messageChannel) {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(definition) {
      const tool = typeof definition === "function"
        ? definition({ messageChannel })
        : definition;
      tools.set(tool.name, tool);
    },
  });
  return tools;
}

test("create tools forward the runtime-provided originating channel", async () => {
  for (const [messageChannel, expected] of [
    ["ios", "app"],
    ["telegram", "telegram"],
    ["line", "line"],
    ["webchat", undefined],
  ]) {
    let requestBody;
    globalThis.fetch = async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return new Response(JSON.stringify({
        state: "approved_queued",
        command_id: "command-test",
      }), { status: 200 });
    };
    const tool = toolsForChannel(messageChannel).get("nbhd_datebook_add_apple_reminder");
    await tool.execute(`call-${messageChannel}`, {
      items: [{ title: "Buy milk" }],
      direct_user_originated: true,
    });
    assert.equal(requestBody.originating_channel, expected);
  }
});
