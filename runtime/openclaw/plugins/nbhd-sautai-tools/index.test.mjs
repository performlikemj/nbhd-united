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

test("registers both sautai tools", () => {
  setupEnv();
  const { api, tools } = buildApi();
  register(api);

  assert.equal(tools.size, 2);
  assert.ok(tools.has("nbhd_generate_meal_plan"));
  assert.ok(tools.has("nbhd_get_meal_plan"));
});

// ── nbhd_generate_meal_plan ────────────────────────────────────────────

test("generate — posts to the sautai generate path with headers and honest text", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-123", status: "pending", week_start: "2026-07-13" } });
  };

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("1", {});

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-NBHD-Internal-Key"], "secret-key");
  assert.equal(calls[0].options.headers["X-NBHD-Tenant-Id"], "tenant-test");
  assert.match(calls[0].url, /\/runtime\/tenant-test\/sautai\/generate-plan\/$/);
  assert.deepEqual(JSON.parse(calls[0].options.body), {});

  // The structured ack rides details.json; the assistant-facing text is the
  // honest "on the way, don't claim it's ready" guidance (not raw JSON).
  assert.equal(result.details.json.job_id, "job-123");
  const text = result.content[0].text;
  assert.match(text, /week of 2026-07-13/);
  assert.match(text, /notification when it's ready/i);
  assert.match(text, /do NOT say the plan is ready/i);
});

test("generate — passes through user_prompt, week_start and number_of_days", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-456", status: "pending" } });
  };

  register(api);
  await tools
    .get("nbhd_generate_meal_plan")
    .execute("2", { user_prompt: "high protein, no pork", week_start: "2026-07-13", number_of_days: 5 });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.user_prompt, "high protein, no pork");
  assert.equal(body.week_start, "2026-07-13");
  assert.equal(body.number_of_days, 5);
});

test("generate — clamps out-of-range number_of_days into 1-7", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-457", status: "pending" } });
  };

  register(api);
  await tools.get("nbhd_generate_meal_plan").execute("2b", { number_of_days: 99 });
  assert.equal(JSON.parse(calls[0].options.body).number_of_days, 7);
});

test("generate — omits blank optional fields", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-789", status: "pending" } });
  };

  register(api);
  await tools.get("nbhd_generate_meal_plan").execute("3", { user_prompt: "   ", week_start: "", number_of_days: "" });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.user_prompt, undefined);
  assert.equal(body.week_start, undefined);
  assert.equal(body.number_of_days, undefined);
});

test("generate — 503 not-configured surfaces a plain 'not configured' message", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({ status: 503, payload: { error: "sautai_not_configured", detail: "sautai integration is not configured" } });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("4", {});
  assert.match(result.content[0].text, /not configured/i);
  // The plain not-configured message is text-only (no structured details leak).
  assert.equal(result.details, undefined);
});

test("generate — other runtime errors are surfaced, not thrown", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ status: 409, payload: { error: "sautai_disabled" } });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("5", {});
  assert.match(result.details.json.error, /NBHD runtime error 409: sautai_disabled/);
  assert.match(result.content[0].text, /Couldn't start the meal plan/);
});

test("generate — missing NBHD_API_BASE_URL surfaces error, not throw", async () => {
  const savedUrl = process.env.NBHD_API_BASE_URL;
  process.env.NBHD_TENANT_ID = "tenant-test";
  process.env.NBHD_INTERNAL_API_KEY = "secret-key";
  delete process.env.NBHD_API_BASE_URL;

  const { api, tools } = buildApi({ pluginConfig: {} });
  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("6", {});
  assert.match(result.details.json.error, /NBHD_API_BASE_URL is required/);

  process.env.NBHD_API_BASE_URL = savedUrl;
});

// ── nbhd_get_meal_plan ─────────────────────────────────────────────────

test("get — posts to the current-plan path and returns a real plan as JSON", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  const plan = { id: 12, week_start: "2026-07-13", days: [{ day: "Monday", meals: [] }] };
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { status: "ok", cached: false, plan, web_link: "https://sautai.com/x" } });
  };

  register(api);
  const result = await tools.get("nbhd_get_meal_plan").execute("7", { week_start: "2026-07-13" });

  assert.match(calls[0].url, /\/runtime\/tenant-test\/sautai\/current-plan\/$/);
  assert.equal(JSON.parse(calls[0].options.body).week_start, "2026-07-13");
  // A current plan is returned verbatim as JSON so the assistant can summarize it.
  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.status, "ok");
  assert.deepEqual(parsed.plan, plan);
});

test("get — no_plan tells the assistant to offer generation", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: { status: "no_plan", week_start: "2026-07-13" } });

  register(api);
  const result = await tools.get("nbhd_get_meal_plan").execute("8", {});
  assert.match(result.content[0].text, /No meal plan exists yet/i);
  assert.match(result.content[0].text, /nbhd_generate_meal_plan/);
  assert.equal(result.details.json.status, "no_plan");
});

test("get — cached fallback is flagged as possibly stale", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({ payload: { status: "ok", cached: true, plan: { id: 9 }, web_link: "" } });

  register(api);
  const result = await tools.get("nbhd_get_meal_plan").execute("9", {});
  assert.match(result.content[0].text, /cached/i);
  assert.equal(result.details.json.cached, true);
});

test("get — 503 not-configured surfaces a plain 'not configured' message", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({ status: 503, payload: { error: "sautai_not_configured", detail: "sautai integration is not configured" } });

  register(api);
  const result = await tools.get("nbhd_get_meal_plan").execute("10", {});
  assert.match(result.content[0].text, /not configured/i);
  assert.equal(result.details, undefined);
});
