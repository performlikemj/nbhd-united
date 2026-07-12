import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

function buildApi({ pluginConfig = { apiBaseUrl: "https://nbhd.test" } } = {}) {
  const tools = new Map();
  const api = {
    pluginConfig,
    registerTool(tool) {
      tools.set(tool.name, tool);
    },
  };
  return { api, tools };
}

function mockResponse({ status = 200, payload = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return JSON.stringify(payload);
    },
  };
}

function setupEnv() {
  process.env.NBHD_TENANT_ID = "tenant-test";
  process.env.NBHD_INTERNAL_API_KEY = "secret-key";
}

test("registers exactly 1 tool", () => {
  setupEnv();
  const { api, tools } = buildApi();
  register(api);

  assert.equal(tools.size, 1);
  assert.ok(tools.has("nbhd_generate_meal_plan"));
});

test("nbhd_generate_meal_plan — happy path posts to the sautai runtime path with headers", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-123", status: "pending" } });
  };

  register(api);
  const tool = tools.get("nbhd_generate_meal_plan");
  assert.ok(tool, "tool should be registered");

  const result = await tool.execute("1", {});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-NBHD-Internal-Key"], "secret-key");
  assert.equal(calls[0].options.headers["X-NBHD-Tenant-Id"], "tenant-test");
  assert.match(calls[0].url, /\/runtime\/tenant-test\/sautai\/generate-plan\/$/);

  const body = JSON.parse(calls[0].options.body);
  assert.deepEqual(body, {});

  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.job_id, "job-123");
  assert.equal(parsed.status, "pending");
});

test("nbhd_generate_meal_plan — passes through user_prompt and week_start when given", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-456", status: "pending" } });
  };

  register(api);
  const tool = tools.get("nbhd_generate_meal_plan");

  await tool.execute("2", { user_prompt: "high protein, no pork", week_start: "2026-07-13" });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.user_prompt, "high protein, no pork");
  assert.equal(body.week_start, "2026-07-13");
});

test("nbhd_generate_meal_plan — omits blank optional fields", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-789", status: "pending" } });
  };

  register(api);
  const tool = tools.get("nbhd_generate_meal_plan");

  await tool.execute("3", { user_prompt: "   ", week_start: "" });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.user_prompt, undefined);
  assert.equal(body.week_start, undefined);
});

test("nbhd_generate_meal_plan — runtime error is surfaced in the payload, not thrown", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 409,
      payload: { error: "sautai_disabled" },
    });

  register(api);
  const tool = tools.get("nbhd_generate_meal_plan");

  const result = await tool.execute("4", {});
  const parsed = JSON.parse(result.content[0].text);
  assert.match(parsed.error, /NBHD runtime error 409: sautai_disabled/);
});

test("nbhd_generate_meal_plan — missing NBHD_API_BASE_URL surfaces error in payload", async () => {
  const savedUrl = process.env.NBHD_API_BASE_URL;
  process.env.NBHD_TENANT_ID = "tenant-test";
  process.env.NBHD_INTERNAL_API_KEY = "secret-key";
  delete process.env.NBHD_API_BASE_URL;

  const { api, tools } = buildApi({ pluginConfig: {} });
  register(api);
  const tool = tools.get("nbhd_generate_meal_plan");

  const result = await tool.execute("5", {});
  const parsed = JSON.parse(result.content[0].text);
  assert.match(parsed.error, /NBHD_API_BASE_URL is required/);

  process.env.NBHD_API_BASE_URL = savedUrl;
});
