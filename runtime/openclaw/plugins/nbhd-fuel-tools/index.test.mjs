import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import register from "./index.js";

function collectTools(pluginConfig = {}) {
  const tools = {};
  register({
    pluginConfig,
    registerTool(definition) {
      tools[definition.name] = definition;
    },
  });
  return tools;
}

function setRuntimeEnv(t) {
  const previousTenantId = process.env.NBHD_TENANT_ID;
  const previousInternalKey = process.env.NBHD_INTERNAL_API_KEY;
  process.env.NBHD_TENANT_ID = "tenant-123";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";
  t.after(() => {
    if (previousTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = previousTenantId;
    if (previousInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = previousInternalKey;
  });
}

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return body;
    },
  };
}

const NEW_TOOLS = ["nbhd_fuel_search_exercises", "nbhd_fuel_get_plan"];

test("manifest includes both catalog-aware read tools and schemas are strict", () => {
  const manifest = JSON.parse(readFileSync(new URL("./openclaw.plugin.json", import.meta.url)));
  const tools = collectTools();
  for (const name of NEW_TOOLS) {
    assert.ok(manifest.contracts.tools.includes(name), name);
    assert.equal(tools[name].parameters.type, "object");
    assert.equal(tools[name].parameters.additionalProperties, false);
  }
  assert.deepEqual(tools.nbhd_fuel_get_plan.parameters.required, ["plan_id"]);
  assert.equal(tools.nbhd_fuel_search_exercises.parameters.properties.limit.maximum, 100);
});

test("search tool encodes query fields, forwards limit, and renders names-only rows", async (t) => {
  setRuntimeEnv(t);
  let captured;
  const payload = {
    results: [
      { name: "Hip flexor stretch", muscle: "Hips", equipment: "Bodyweight", stretch: true },
      { name: "Romanian Deadlift", muscle: "Hamstrings", equipment: "Barbell", stretch: false },
    ],
    total: 2,
    muscles: ["Hamstrings", "Hips"],
    equipment_types: ["Barbell", "Bodyweight"],
    guidance: "Use names verbatim.",
  };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url: new URL(url), options };
    return response(200, JSON.stringify(payload));
  });

  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" })
    .nbhd_fuel_search_exercises.execute("call-1", {
      query: "rear delt & arms",
      muscle: "Rear Delts",
      equipment: "Resistance Band",
      limit: 77,
    });

  assert.equal(captured.url.pathname, "/api/v1/fuel/runtime/tenant-123/exercises/");
  assert.equal(captured.url.searchParams.get("q"), "rear delt & arms");
  assert.equal(captured.url.searchParams.get("muscle"), "Rear Delts");
  assert.equal(captured.url.searchParams.get("equipment"), "Resistance Band");
  assert.equal(captured.url.searchParams.get("limit"), "77");
  assert.equal(captured.options.method, "GET");
  assert.deepEqual(result.details.json, payload);
  assert.match(result.content[0].text, /Hip flexor stretch — Hips · Bodyweight · stretch/);
  assert.match(result.content[0].text, /total: 2/);
  for (const forbidden of ["slug", "frames", "image", "asset"]) {
    assert.doesNotMatch(result.content[0].text.toLowerCase(), new RegExp(forbidden));
    assert.equal(Object.hasOwn(result.details.json.results[0], forbidden), false);
  }
});

test("get_plan encodes id and renders compact rows grouped by week", async (t) => {
  setRuntimeEnv(t);
  let captured;
  const payload = {
    name: "Strength Block",
    status: "active",
    start_date: "2026-04-27",
    workouts: [
      { date: "2026-04-27", activity: "Push", status: "planned", has_prescription: true },
      { date: "2026-05-04", activity: "Push", status: "planned", has_prescription: false },
      { date: "2026-05-05", activity: "Rest day", status: "rest", has_prescription: null },
    ],
  };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url: new URL(url), options };
    return response(200, JSON.stringify(payload));
  });

  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" })
    .nbhd_fuel_get_plan.execute("call-1", { plan_id: "plan/id" });

  assert.equal(captured.url.pathname, "/api/v1/fuel/runtime/tenant-123/plans/plan%2Fid/");
  assert.equal(captured.options.method, "GET");
  assert.deepEqual(result.details.json, payload);
  assert.match(
    result.content[0].text,
    /prescription legend: yes \(has_prescription true\) = filled · no \(false\) = needs filling · rest \(null\) = skip/,
  );
  assert.match(result.content[0].text, /Week 1\n2026-04-27 · Push · planned · prescription yes/);
  assert.match(result.content[0].text, /Week 2\n2026-05-04 · Push · planned · prescription no/);
  assert.match(result.content[0].text, /2026-05-05 · Rest day · rest · prescription rest/);
});

test("audit renders null prescription rest rows as rest with the tri-state legend", async (t) => {
  setRuntimeEnv(t);
  const payload = {
    today_plan: { exists: false, workouts: [] },
    next_14d_workouts: [
      { date: "2026-05-05", activity: "Rest day", status: "rest", has_prescription: null },
      { date: "2026-05-06", activity: "Pull", status: "planned", has_prescription: false },
    ],
    conflicts: { duplicate_fires: [], orphan_crons: [], orphan_workouts: [] },
    guidance: "Keep the programmed rest day.",
  };
  t.mock.method(globalThis, "fetch", async () => response(200, JSON.stringify(payload)));

  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" })
    .nbhd_fuel_audit.execute("call-audit", {});

  assert.deepEqual(result.details.json, payload);
  assert.match(result.content[0].text, /rest \(null\) = skip/);
  assert.match(result.content[0].text, /2026-05-05 · Rest day · rest · prescription rest/);
  assert.match(result.content[0].text, /2026-05-06 · Pull · planned · prescription no/);
});

test("update_workout mobility guidance uses catalog skills and reserves blocks", () => {
  const description = collectTools()
    .nbhd_fuel_update_workout.parameters.properties.detail_json.description;
  assert.match(description, /catalog-named skills with hold_time sets/);
  assert.match(description, /blocks only for non-movement work/);
  assert.doesNotMatch(description, /For mobility, set \{"blocks"/);
});

test("both new tools surface transport failures", async (t) => {
  setRuntimeEnv(t);
  t.mock.method(globalThis, "fetch", async () => {
    throw new Error("network down");
  });
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });
  const search = await tools.nbhd_fuel_search_exercises.execute("call-1", {});
  const plan = await tools.nbhd_fuel_get_plan.execute("call-2", { plan_id: "plan-1" });
  assert.equal(search.details.json.error, "network down");
  assert.equal(plan.details.json.error, "network down");
});

test("both new tools handle successful non-JSON responses", async (t) => {
  setRuntimeEnv(t);
  t.mock.method(globalThis, "fetch", async () => response(200, "not-json"));
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });
  const search = await tools.nbhd_fuel_search_exercises.execute("call-1", {});
  const plan = await tools.nbhd_fuel_get_plan.execute("call-2", { plan_id: "plan-1" });
  assert.deepEqual(search.details.json, { detail: "upstream returned a non-JSON response body" });
  assert.deepEqual(plan.details.json, { detail: "upstream returned a non-JSON response body" });
});

test("both new tools preserve compact 4xx errors", async (t) => {
  setRuntimeEnv(t);
  t.mock.method(globalThis, "fetch", async () =>
    response(400, JSON.stringify({ error: "invalid_filter", detail: "bad value" })),
  );
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });
  const search = await tools.nbhd_fuel_search_exercises.execute("call-1", {});
  const plan = await tools.nbhd_fuel_get_plan.execute("call-2", { plan_id: "plan-1" });
  assert.equal(search.details.json.error, "NBHD runtime error 400: invalid_filter (bad value)");
  assert.equal(plan.details.json.error, "NBHD runtime error 400: invalid_filter (bad value)");
});

test("plan rotation and catalog errors stay structured instead of flattening", async (t) => {
  setRuntimeEnv(t);
  const payload = {
    error: "plan_rotation_required",
    message: "Rotate recipes",
    tracks: [{ weekday: "monday", activity: "Push", weeks: [1, 2, 3], max_consecutive_same: 3 }],
    week_overrides_semantics: "whole_map_replacement",
    catalog_candidates: [],
  };
  t.mock.method(globalThis, "fetch", async () => response(400, JSON.stringify(payload)));
  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" })
    .nbhd_fuel_create_plan.execute("call", {
      name: "Plan",
      weeks: 8,
      days_per_week: 1,
      schedule_json: { monday: {} },
    });
  assert.deepEqual(result.details.json, payload);
  assert.equal(result.content[0].text, JSON.stringify(payload));
  assert.doesNotMatch(result.content[0].text, /NBHD runtime error/);
});

test("plan tools expose roles and forward explicit repeat policies", async (t) => {
  setRuntimeEnv(t);
  let body;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    body = JSON.parse(options.body);
    return response(200, JSON.stringify({ id: "plan-1" }));
  });
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });
  assert.match(
    tools.nbhd_fuel_create_plan.parameters.properties.schedule_json.additionalProperties.properties.detail_json.description,
    /primary \| accessory \| warmup \| mobility/,
  );
  await tools.nbhd_fuel_create_plan.execute("call", {
    name: "Rehab",
    weeks: 8,
    days_per_week: 1,
    schedule_json: { monday: {} },
    repeat_policy: "intentional",
    repeat_reason: "Fixed rehab block",
  });
  assert.equal(body.repeat_policy, "intentional");
  assert.equal(body.repeat_reason, "Fixed rehab block");
});

test("all four write tools render the unmatched exercise warning line", async (t) => {
  setRuntimeEnv(t);
  t.mock.method(globalThis, "fetch", async () =>
    response(200, JSON.stringify({ id: "row-1", unmatched_exercises: ["Mystery Curl", "Odd Press"] })),
  );
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });
  const cases = [
    ["nbhd_fuel_log_workout", { activity: "Workout" }],
    ["nbhd_fuel_update_workout", { workout_id: "workout-1" }],
    ["nbhd_fuel_create_plan", { name: "Plan", weeks: 1, days_per_week: 1, schedule_json: {} }],
    ["nbhd_fuel_update_plan", { plan_id: "plan-1" }],
  ];
  for (const [name, params] of cases) {
    const result = await tools[name].execute("call", params);
    assert.match(
      result.content[0].text,
      /No figure for: Mystery Curl, Odd Press — for movements you chose, use exact catalog names \(nbhd_fuel_search_exercises\); never swap a user-requested movement without asking/,
      name,
    );
  }
});

test("write tools render catalog matches from their own local request", async (t) => {
  setRuntimeEnv(t);
  t.mock.method(globalThis, "fetch", async () =>
    response(200, JSON.stringify({
      id: "row-1",
      catalog_matches: [{
        loc: ["detail_json", "exercises", 0, "name"],
        slug: "hammer-curl",
        matched_by: "equipment_prefix",
        name: "Hammer Curl",
      }],
    })),
  );
  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" })
    .nbhd_fuel_log_workout.execute("call", {
      activity: "Arms",
      category: "strength",
      detail_json: { exercises: [{ name: "Dumbbell Hammer Curls", sets: [] }] },
    });
  assert.match(result.content[0].text, /figure: Hammer Curl ← "Dumbbell Hammer Curls"/);
  assert.equal(result.details.json.catalog_matches[0].slug, "hammer-curl");
});

test("nbhd_fuel_update_plan exposes explicit schedule removal fields", () => {
  const tool = collectTools().nbhd_fuel_update_plan;
  const properties = tool.parameters.properties;

  assert.equal(properties.remove_days.type, "array");
  assert.equal(properties.remove_days.items.oneOf[0].type, "string");
  assert.equal(properties.remove_days.items.oneOf[1].type, "integer");
  assert.equal(properties.replace_schedule.type, "boolean");
  assert.match(tool.description, /schedule_json MERGES by default/);
  assert.match(properties.schedule_json.description, /weekends without touching weekdays/);
});

test("nbhd_fuel_update_plan forwards remove_days and replace_schedule", async (t) => {
  const previousTenantId = process.env.NBHD_TENANT_ID;
  const previousInternalKey = process.env.NBHD_INTERNAL_API_KEY;
  process.env.NBHD_TENANT_ID = "tenant-123";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";
  t.after(() => {
    if (previousTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = previousTenantId;
    if (previousInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = previousInternalKey;
  });

  let captured;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url: new URL(url), options };
    return {
      ok: true,
      status: 200,
      async text() {
        return JSON.stringify({ updated: true });
      },
    };
  });

  const tool = collectTools({ apiBaseUrl: "https://nbhd.example" }).nbhd_fuel_update_plan;
  const result = await tool.execute("call-1", {
    plan_id: "plan-123",
    schedule_json: {
      saturday: { category: "mobility", activity: "Mobility" },
    },
    remove_days: ["friday", 3],
    replace_schedule: true,
  });

  assert.equal(captured.url.pathname, "/api/v1/fuel/runtime/tenant-123/plans/plan-123/");
  assert.equal(captured.options.method, "PATCH");
  assert.deepEqual(JSON.parse(captured.options.body), {
    schedule_json: {
      saturday: { category: "mobility", activity: "Mobility" },
    },
    remove_days: ["friday", 3],
    replace_schedule: true,
  });
  assert.deepEqual(result.details.json, { updated: true });
});
