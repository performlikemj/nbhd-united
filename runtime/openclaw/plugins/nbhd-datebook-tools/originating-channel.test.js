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

function toolsForContext(toolContext) {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(definition) {
      const tool = typeof definition === "function"
        ? definition(toolContext)
        : definition;
      tools.set(tool.name, tool);
    },
  });
  return tools;
}

test("create tools forward the runtime-provided originating channel", async () => {
  for (const [label, toolContext, expected] of [
    ["future-ios-context", { messageChannel: "ios" }, "app"],
    ["telegram", { messageChannel: "telegram" }, "telegram"],
    ["line", { messageChannel: "line" }, "line"],
    [
      "pinned-ios-context",
      {
        messageChannel: undefined,
        sessionKey: "agent:main:openai-user:thread:00000000-0000-4000-8000-000000000001",
      },
      "app",
    ],
    ["legacy", { messageChannel: "webchat", sessionKey: "agent:main:openai-user:background" }, undefined],
    [
      "different-agent",
      { sessionKey: "agent:assistant:openai-user:thread:00000000-0000-4000-8000-000000000001" },
      undefined,
    ],
  ]) {
    let requestBody;
    globalThis.fetch = async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return new Response(JSON.stringify({
        state: "approved_queued",
        command_id: "command-test",
      }), { status: 200 });
    };
    const tool = toolsForContext(toolContext).get("nbhd_datebook_add_apple_reminder");
    await tool.execute(`call-${label}`, {
      items: [{ title: "Buy milk" }],
      direct_user_originated: true,
    });
    assert.equal(requestBody.originating_channel, expected);
  }
});

test("app-surface creates return server guidance without polling command status", async () => {
  const guidance = "Waiting for your approval; the approval is in this conversation. Review it within 24 hours.";
  const requestPaths = [];
  globalThis.fetch = async (url) => {
    const requestPath = new URL(url).pathname;
    requestPaths.push(requestPath);
    if (requestPath.endsWith("/datebook/request-create")) {
      return new Response(JSON.stringify({
        state: "approval_pending",
        command_id: "command-app",
        approval_surface: "app",
        delivery_state: "available",
        guidance,
      }), { status: 202 });
    }
    return new Response(JSON.stringify({
      state: "approved_queued",
      command_id: "command-app",
      guidance: "Unexpected command-status poll.",
    }), { status: 200 });
  };

  const tool = toolsForContext({ messageChannel: "ios" }).get("nbhd_datebook_add_apple_reminder");
  const result = await tool.execute("call-guidance", {
    items: [{ title: "Buy milk" }],
    direct_user_originated: true,
  });

  assert.equal(result.content[0].text, guidance);
  assert.equal(result.details.approval_surface, "app");
  assert.equal(result.details.delivery_state, "available");
  assert.deepEqual(requestPaths, [
    "/api/v1/datebook/runtime/tenant-test/datebook/request-create",
  ]);
});

test("telegram-surface creates keep polling command status", async () => {
  const guidance = "Approved and queued for delivery.";
  const requestPaths = [];
  globalThis.fetch = async (url) => {
    const requestPath = new URL(url).pathname;
    requestPaths.push(requestPath);
    if (requestPath.endsWith("/datebook/request-create")) {
      return new Response(JSON.stringify({
        state: "approval_pending",
        command_id: "command-telegram",
        approval_surface: "telegram",
        delivery_state: "available",
      }), { status: 202 });
    }
    return new Response(JSON.stringify({
      state: "approved_queued",
      command_id: "command-telegram",
      approval_surface: "telegram",
      delivery_state: "delivered",
      guidance,
    }), { status: 200 });
  };

  const tool = toolsForContext({ messageChannel: "telegram" }).get("nbhd_datebook_add_apple_reminder");
  const result = await tool.execute("call-telegram-poll", {
    items: [{ title: "Buy milk" }],
    direct_user_originated: true,
  });

  assert.equal(result.content[0].text, guidance);
  assert.equal(result.details.state, "approved_queued");
  assert.deepEqual(requestPaths, [
    "/api/v1/datebook/runtime/tenant-test/datebook/request-create",
    "/api/v1/datebook/runtime/tenant-test/datebook/command-status/command-telegram",
  ]);
});
