import assert from "node:assert/strict";
import { after, test } from "node:test";

import register from "./index.js";

const previous = {
  base: process.env.NBHD_API_BASE_URL,
  tenant: process.env.NBHD_TENANT_ID,
  key: process.env.NBHD_INTERNAL_API_KEY,
};
const originalFetch = globalThis.fetch;
process.env.NBHD_API_BASE_URL = "https://automation.invalid";
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

function registeredTools() {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(tool) {
      tools.set(tool.name, tool);
    },
  });
  return tools;
}

const origin = {
  v: 1,
  kind: "cron",
  tenant_id: "tenant-test",
  run_id: "run-1",
  job_id: "job-1",
  ts: 1_800_000_000,
  sig: "abc123",
};

const cases = [
  ["nbhd_cron_create_pure_reminder", {
    name: "Pure",
    schedule: { kind: "cron", expr: "0 8 * * *" },
    text: "Drink water",
  }],
  ["nbhd_cron_create_quote_user_intent", {
    name: "Quote",
    schedule: { kind: "cron", expr: "0 9 * * 1" },
    text: "Plan the week",
    refresh_facts_via: "nbhd_task_list",
  }],
  ["nbhd_cron_create_domain_summary", {
    name: "Summary",
    schedule: { kind: "cron", expr: "0 10 * * 0" },
    query_tool: "nbhd_task_list",
    query_args: { status: "open" },
    render_block: "task_summary",
  }],
];

test("all cron-create tools forward origin and the OpenClaw tool-call id", async () => {
  const tools = registeredTools();
  for (const [index, [name, params]] of cases.entries()) {
    let body;
    globalThis.fetch = async (_url, options) => {
      body = JSON.parse(options.body);
      return new Response(JSON.stringify({ id: `cron-${index}`, name: params.name }), { status: 201 });
    };
    const tool = tools.get(name);
    const result = await tool.execute(`call-${index}`, { ...params, _nbhd_origin: origin });
    assert.equal(body.cron_request_id, `call-${index}`, name);
    assert.deepEqual(body.origin, origin, name);
    assert.equal(result.details.json.id, `cron-${index}`, name);
    assert.match(result.content[0].text, new RegExp(`cron-${index}`), name);
    assert.equal(Object.hasOwn(tool.parameters.properties, "_nbhd_origin"), false, name);
  }
});

test("null and other invalid hidden origin values are not forwarded", async () => {
  const tool = registeredTools().get("nbhd_cron_create_pure_reminder");
  for (const invalidOrigin of [null, "forged", []]) {
    let body;
    globalThis.fetch = async (_url, options) => {
      body = JSON.parse(options.body);
      return new Response(JSON.stringify({ id: "cron-plain" }), { status: 201 });
    };
    await tool.execute("call-plain", { ...cases[0][1], _nbhd_origin: invalidOrigin });
    assert.equal(Object.hasOwn(body, "origin"), false);
  }
});

test("202 pending approval result is explicit that the task does not exist", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    state: "pending_approval",
    action_id: 42,
    summary: "Every Friday at 5pm",
  }), { status: 202 });
  const tool = registeredTools().get("nbhd_cron_create_pure_reminder");
  const result = await tool.execute("call-pending", cases[0][1]);
  assert.match(result.content[0].text, /pending the user's approval/i);
  assert.match(result.content[0].text, /does not exist yet/i);
  assert.equal(result.details.json.action_id, 42);
});

test("409 request-id and name conflicts return clear, non-creation text", async () => {
  const tool = registeredTools().get("nbhd_cron_create_pure_reminder");
  for (const [code, expected] of [
    ["request_id_conflict", /same request ID/i],
    ["name_conflict", /with this name already exists/i],
  ]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ error: code }), { status: 409 });
    const result = await tool.execute(`call-${code}`, cases[0][1]);
    assert.match(result.content[0].text, expected);
    assert.match(result.content[0].text, /Nothing new was created/i);
    assert.equal(result.details.error, code);
  }
});
