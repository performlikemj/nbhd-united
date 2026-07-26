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

test("schemas expose server-resolved week targeting and confirmation", () => {
  setupEnv();
  const { api, tools } = buildApi();
  register(api);

  for (const name of ["nbhd_generate_meal_plan", "nbhd_get_meal_plan"]) {
    const week = tools.get(name).parameters.properties.week;
    assert.deepEqual(week.enum, ["current", "next"]);
    assert.match(tools.get(name).parameters.properties.week_start.description, /explicitly names a date/i);
  }
  assert.equal(tools.get("nbhd_generate_meal_plan").parameters.properties.week.default, undefined);
  assert.match(tools.get("nbhd_generate_meal_plan").parameters.properties.week.description, /Saturday-Sunday/i);
  assert.equal(tools.get("nbhd_get_meal_plan").parameters.properties.week.default, "current");
  assert.ok(tools.get("nbhd_generate_meal_plan").parameters.properties.confirm_replace);
  assert.ok(tools.get("nbhd_generate_meal_plan").parameters.properties.confirm_token);
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
  assert.match(text, /1–2 minutes/i);
  assert.match(text, /nbhd_get_meal_plan later/i);
  assert.match(text, /do NOT say the plan is ready/i);
});

test("generate — passes through guidance, week targeting, confirmation and day count", async () => {
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
    .execute("2", {
      user_prompt: "high protein, no pork",
      week: "next",
      week_start: "2026-07-13",
      number_of_days: 5,
      regenerate: true,
      confirm_replace: true,
      confirm_token: "signed-preview-token",
    });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.user_prompt, "high protein, no pork");
  assert.equal(body.week, "next");
  assert.equal(body.week_start, "2026-07-13");
  assert.equal(body.number_of_days, 5);
  assert.equal(body.regenerate, true);
  assert.equal(body.confirm_replace, true);
  assert.equal(body.confirm_token, "signed-preview-token");
});

test("generate — preview tells the assistant to wait for user verification", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      payload: {
        status: "confirmation_required",
        confirm_token: "signed-preview-token",
        preview: {
          week_start: "2026-07-27",
          week_end: "2026-08-02",
          confirmation_message:
            "Send this meal-plan request to sautai for Monday, July 27, 2026 through Sunday, August 2, 2026?",
          tool_parameters: { week_start: "2026-07-27", number_of_days: 7 },
        },
      },
    });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("preview", {});
  assert.match(result.content[0].text, /Monday, July 27, 2026 through Sunday, August 2, 2026/);
  assert.match(result.content[0].text, /Do NOT send or start generation yet/i);
  assert.match(result.content[0].text, /explicit verification/i);
  assert.equal(result.details.json.confirm_token, "signed-preview-token");
});

test("generate — passes regenerate=true through, omits it otherwise", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { job_id: "job-r", status: "pending" } });
  };

  register(api);
  const tool = tools.get("nbhd_generate_meal_plan");

  await tool.execute("2r", { regenerate: true });
  assert.equal(JSON.parse(calls[0].options.body).regenerate, true);

  // Anything not strictly true is omitted (default keep-existing behavior).
  await tool.execute("2r2", { regenerate: false });
  assert.equal(JSON.parse(calls[1].options.body).regenerate, undefined);
  await tool.execute("2r3", {});
  assert.equal(JSON.parse(calls[2].options.body).regenerate, undefined);
});

test("generate — coalesced request_applied:false yields an honest 'not applied' copy", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      payload: { job_id: "job-c", status: "generating", week_start: "2026-07-13", request_applied: false },
    });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("2c", { regenerate: true });
  assert.match(result.content[0].text, /ALREADY being generated/i);
  assert.match(result.content[0].text, /ask whether they want it replaced/i);
  assert.match(result.content[0].text, /do NOT claim their new guidance was applied/i);
});

test("generate — confirm_required tells the assistant to ask before replacement", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      payload: {
        status: "confirm_required",
        week_start: "2026-07-13",
        plan: { id: 42 },
        web_link: "https://sautai.test/existing",
      },
    });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("confirm", { regenerate: true });
  assert.match(result.content[0].text, /explicitly confirm/i);
  assert.match(result.content[0].text, /Do NOT regenerate yet/i);
  assert.match(result.content[0].text, /confirm_replace=true/i);
  assert.equal(result.details.json.plan.id, 42);
});

test("generate — exists says prompt guidance was not applied and offers regeneration", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({ payload: { status: "exists", week_start: "2026-07-13", plan: { id: 43 } } });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("exists", { user_prompt: "more veg" });
  assert.match(result.content[0].text, /guidance was NOT applied/i);
  assert.match(result.content[0].text, /offer to regenerate/i);
  assert.match(result.content[0].text, /explicit user confirmation/i);
});

test("generate — promptless exists surfaces the plan without claiming guidance was dropped", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      payload: {
        status: "exists",
        week_start: "2026-07-13",
        plan: { id: 44 },
        guidance: "A plan already exists for this week. Surface the existing plan.",
      },
    });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("exists-promptless", {});
  assert.match(result.content[0].text, /meal plan already exists/i);
  assert.match(result.content[0].text, /surface the existing plan/i);
  assert.match(result.content[0].text, /only offer to regenerate it if the user seems to want a new plan/i);
  assert.doesNotMatch(result.content[0].text, /guidance was NOT applied/i);
  assert.equal(result.details.json.plan.id, 44);
});

test("generate — incomplete-plan repair says missing days are filling without touching meals", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 201,
      payload: {
        job_id: "job-repair",
        status: "pending",
        week_start: "2026-07-13",
        repairing_incomplete_plan: true,
        repairing_missing_days: ["2026-07-15"],
      },
    });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("repair", {});
  assert.match(result.content[0].text, /filling in the missing days/i);
  assert.match(result.content[0].text, /2026-07-15/);
  assert.match(result.content[0].text, /existing meals will be left untouched/i);
  assert.match(result.content[0].text, /do NOT say the week is complete yet/i);
});

test("generate — request_applied omitted uses the normal started copy", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({ payload: { job_id: "job-n", status: "pending", week_start: "2026-07-13" } });

  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("2n", {});
  assert.match(result.content[0].text, /generation started/i);
  assert.doesNotMatch(result.content[0].text, /ALREADY being generated/i);
});

test("generate — response carries the powered-by-sautai attribution", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: { job_id: "job-a", status: "pending", week_start: "2026-07-13" } });
  register(api);
  const result = await tools.get("nbhd_generate_meal_plan").execute("2a", {});
  assert.match(result.content[0].text, /powered by sautai/i);
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

test("generate — preserves prompt text verbatim and omits empty optional fields", async () => {
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
  assert.equal(body.user_prompt, "   ");
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
  const result = await tools
    .get("nbhd_get_meal_plan")
    .execute("7", { week: "next", week_start: "2026-07-13" });

  assert.match(calls[0].url, /\/runtime\/tenant-test\/sautai\/current-plan\/$/);
  assert.deepEqual(JSON.parse(calls[0].options.body), { week: "next", week_start: "2026-07-13" });
  // A current plan is returned verbatim as JSON so the assistant can summarize it.
  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.status, "ok");
  assert.deepEqual(parsed.plan, plan);
});

test("get — partial plan names missing days and forbids presenting a complete week", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      payload: {
        status: "ok",
        cached: false,
        complete: false,
        missing_days: ["2026-07-15", "2026-07-17"],
        plan: { id: 13, week_start: "2026-07-13" },
      },
    });

  register(api);
  const result = await tools.get("nbhd_get_meal_plan").execute("partial", {});
  assert.match(result.content[0].text, /partial/i);
  assert.match(result.content[0].text, /2026-07-15/);
  assert.match(result.content[0].text, /2026-07-17/);
  assert.match(result.content[0].text, /never present this partial week as complete/i);
  assert.deepEqual(result.details.json.missing_days, ["2026-07-15", "2026-07-17"]);
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

test("get — generation_in_progress explains the async wait and later fetch", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      payload: {
        status: "no_plan",
        week_start: "2026-07-20",
        generation_in_progress: { week_start: "2026-07-20", seconds_since_started: 35 },
      },
    });

  register(api);
  const result = await tools.get("nbhd_get_meal_plan").execute("progress", { week: "next" });
  assert.match(result.content[0].text, /still running/i);
  assert.match(result.content[0].text, /1–2 minutes/i);
  assert.match(result.content[0].text, /notification/i);
  assert.match(result.content[0].text, /nbhd_get_meal_plan later/i);
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
