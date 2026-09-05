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

test("log_workout description carries natural-language logging defaults", () => {
  const tool = collectTools().nbhd_fuel_log_workout;
  assert.match(tool.description, /if unknown, use "other"/);
  assert.match(tool.description, /Default to today's date and status "done"/);
  assert.match(tool.description, /do not interrogate the user for missing optional fields/);
  assert.match(tool.description, /one call for a mixed session containing weighted exercises and holds/);
  assert.match(tool.parameters.properties.date.description, /relative phrase like 'yesterday'\/'last Tuesday'/);
});

test("log_body_weight description carries scalar capture and conversion rules", () => {
  const tool = collectTools().nbhd_fuel_log_body_weight;
  assert.match(tool.description, /without asking permission/);
  assert.match(tool.description, /one call per scalar measurement/i);
  assert.match(tool.description, /Clarify or skip fuzzy ranges/);
  assert.match(tool.parameters.properties.weight_kg.description, /lbs \/ 2\.2046/);
  assert.match(tool.parameters.properties.date.description, /relative phrase like 'yesterday'\/'last Tuesday'/);
});

test("Fuel delete tools expose the preview confirmation contract", () => {
  const tools = collectTools();
  for (const name of [
    "nbhd_fuel_delete_workout",
    "nbhd_fuel_delete_body_weight",
    "nbhd_fuel_delete_plan",
  ]) {
    assert.match(tools[name].description, /first call returns a preview \+ confirm_token/i);
    assert.equal(tools[name].parameters.properties.confirm_token.type, "string");
    assert.equal(tools[name].parameters.required.includes("confirm_token"), false);
  }
});

test("Fuel delete tools forward confirm_token in DELETE bodies", async (t) => {
  setRuntimeEnv(t);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return response(200, "{}");
  });
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });

  await tools.nbhd_fuel_delete_workout.execute("workout-delete", {
    workout_id: "workout-1",
    confirm_token: "workout-token",
  });
  await tools.nbhd_fuel_delete_body_weight.execute("weight-delete", {
    date: "2026-08-28",
    confirm_token: "weight-token",
  });
  await tools.nbhd_fuel_delete_plan.execute("plan-delete", {
    plan_id: "plan-1",
    confirm_token: "plan-token",
  });

  assert.deepEqual(calls.map(({ options }) => JSON.parse(options.body)), [
    { confirm_token: "workout-token" },
    { confirm_token: "weight-token" },
    { confirm_token: "plan-token" },
  ]);
  assert.match(calls[1].url, /body-weight\/\?date=2026-08-28$/);
});

test("update_profile description carries progressive onboarding completion and decline rules", () => {
  const description = collectTools().nbhd_fuel_update_profile.description;
  assert.match(description, /Save answers as they are learned during onboarding/);
  assert.match(description, /After the onboarding questions, set onboarding_status to 'completed'/);
  assert.match(description, /set it to 'declined'/);
  assert.match(description, /never nag them to resume/);
});

test("plan tool descriptions carry context, rationale, and rest-day rules", () => {
  const tools = collectTools();
  for (const name of ["nbhd_fuel_create_plan", "nbhd_fuel_update_plan"]) {
    const description = tools[name].description;
    assert.match(description, /nbhd_fuel_summary/);
    assert.match(description, /nbhd_lesson_search/);
    assert.match(description, /nbhd_journal_search/);
    assert.match(description, /contextual programming rationale/);
    assert.match(description, /omit rest days/);
  }
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
    tracks: [{ weekday: "monday", category: "strength", weeks: [1, 2, 3], max_consecutive_same: 3 }],
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

function rotationDay(activity = "Upper", accessories = ["Base Curl"]) {
  return {
    activity,
    category: "strength",
    detail_json: {
      exercises: [
        { name: "Bench Press", role: "primary", sets: [{ type: "weighted_reps", reps: 5, weight: 60 }] },
        ...accessories.map((name) => ({
          name,
          role: "accessory",
          sets: [{ type: "weighted_reps", reps: 10, weight: 10 }],
        })),
      ],
    },
  };
}

test("create_plan compiles rotations across plural weeks, existing overrides, and rest weeks", async (t) => {
  setRuntimeEnv(t);
  let captured;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    captured = JSON.parse(options.body);
    return response(201, JSON.stringify({ id: "plan-1" }));
  });
  await collectTools({ apiBaseUrl: "https://nbhd.example" }).nbhd_fuel_create_plan.execute("call", {
    name: "Rotating",
    weeks: 5,
    days_per_week: 1,
    schedule_json: { monday: rotationDay() },
    week_overrides: {
      1: { monday: rotationDay("Custom Monday"), tuesday: rotationDay("Keep Tuesday") },
      2: { monday: null },
    },
    accessory_rotations: [{
      weekday: "monday",
      slot: { exercise_index: 1 },
      every_weeks: 2,
      choices: [
        { name: "Hammer Curl", sets: [{ type: "weighted_reps", reps: 10, weight: 12 }] },
        { name: "Front Raise", sets: [{ type: "weighted_reps", reps: 12, weight: 8 }] },
      ],
    }],
  });

  assert.equal(Object.hasOwn(captured, "accessory_rotations"), false);
  assert.equal(captured._compiled_rotations, 4);
  assert.equal(captured.schedule_json.monday.detail_json.exercises[1].name, "Base Curl");
  assert.equal(captured.week_overrides["0"].monday.detail_json.exercises[1].name, "Hammer Curl");
  assert.equal(captured.week_overrides["1"].monday.activity, "Custom Monday");
  assert.equal(captured.week_overrides["1"].monday.detail_json.exercises[1].name, "Hammer Curl");
  assert.equal(captured.week_overrides["1"].tuesday.activity, "Keep Tuesday");
  assert.equal(captured.week_overrides["2"].monday, null);
  assert.equal(captured.week_overrides["3"].monday.detail_json.exercises[1].name, "Front Raise");
  assert.equal(captured.week_overrides["4"].monday.detail_json.exercises[1].name, "Hammer Curl");
  assert.equal(captured.week_overrides["0"].monday.detail_json.exercises[1].role, "accessory");
});

test("two role-addressed rotations compose on one day", async (t) => {
  setRuntimeEnv(t);
  let captured;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    captured = JSON.parse(options.body);
    return response(201, JSON.stringify({ id: "plan-1" }));
  });
  await collectTools({ apiBaseUrl: "https://nbhd.example" }).nbhd_fuel_create_plan.execute("call", {
    name: "Two slots",
    weeks: 2,
    days_per_week: 1,
    schedule_json: { monday: rotationDay("Upper", ["Curl", "Raise"]) },
    accessory_rotations: [
      {
        weekday: "monday",
        slot: { role: "accessory", nth: 0 },
        every_weeks: 1,
        choices: [{ name: "Hammer Curl", sets: [] }, { name: "Cable Curl", sets: [] }],
      },
      {
        weekday: "monday",
        slot: { role: "accessory", nth: 1 },
        every_weeks: 1,
        choices: [{ name: "Front Raise", sets: [] }, { name: "Face Pull", sets: [] }],
      },
    ],
  });
  assert.deepEqual(
    captured.week_overrides["0"].monday.detail_json.exercises.slice(1).map((item) => item.name),
    ["Hammer Curl", "Front Raise"],
  );
  assert.deepEqual(
    captured.week_overrides["1"].monday.detail_json.exercises.slice(1).map((item) => item.name),
    ["Cable Curl", "Face Pull"],
  );
});

test("update_plan compiles from the stored plan and sends one complete override map", async (t) => {
  setRuntimeEnv(t);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    calls.push(options);
    if (options.method === "GET") {
      return response(200, JSON.stringify({
        weeks: 3,
        schedule_json: { monday: rotationDay() },
        week_overrides: { 1: { tuesday: rotationDay("Keep") } },
      }));
    }
    return response(200, JSON.stringify({ id: "plan-1" }));
  });
  await collectTools({ apiBaseUrl: "https://nbhd.example" }).nbhd_fuel_update_plan.execute("call", {
    plan_id: "plan-1",
    accessory_rotations: [{
      weekday: "monday",
      slot: { role: "accessory", nth: 0 },
      every_weeks: 1,
      choices: [{ name: "Hammer Curl", sets: [] }, { name: "Cable Curl", sets: [] }],
    }],
  });
  assert.equal(calls.length, 2);
  const patchBody = JSON.parse(calls[1].body);
  assert.equal(Object.hasOwn(patchBody, "accessory_rotations"), false);
  assert.equal(patchBody.week_overrides["1"].tuesday.activity, "Keep");
  assert.equal(patchBody.week_overrides["2"].monday.detail_json.exercises[1].name, "Hammer Curl");
});

test("rotation slot out of range returns a clear error without calling runtime", async (t) => {
  setRuntimeEnv(t);
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    return response(201, "{}");
  });
  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" })
    .nbhd_fuel_create_plan.execute("call", {
      name: "Bad slot",
      weeks: 2,
      days_per_week: 1,
      schedule_json: { monday: rotationDay() },
      accessory_rotations: [{
        weekday: "monday",
        slot: { exercise_index: 99 },
        every_weeks: 1,
        choices: [{ name: "Hammer Curl", sets: [] }],
      }],
    });
  assert.equal(calls, 0);
  assert.match(result.details.json.error, /slot is out of range for monday/);
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

test("successful search marks only the next plan or workout write", async (t) => {
  setRuntimeEnv(t);
  const writes = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/exercises/")) {
      return response(200, JSON.stringify({ results: [], total: 0 }));
    }
    writes.push(JSON.parse(options.body));
    return response(200, JSON.stringify({ id: "row-1" }));
  });
  const tools = collectTools({ apiBaseUrl: "https://nbhd.example" });

  await tools.nbhd_fuel_search_exercises.execute("search-1", { query: "curl" });
  await tools.nbhd_fuel_log_workout.execute("write-1", { activity: "Arms" });
  await tools.nbhd_fuel_update_workout.execute("write-2", { workout_id: "workout-1", notes: "done" });
  await tools.nbhd_fuel_search_exercises.execute("search-2", { muscle: "Hips" });
  await tools.nbhd_fuel_create_plan.execute("write-3", {
    name: "Plan",
    weeks: 1,
    days_per_week: 1,
    schedule_json: { monday: {} },
  });
  await tools.nbhd_fuel_update_plan.execute("write-4", { plan_id: "plan-1", notes: "updated" });

  assert.deepEqual(writes.map((body) => body._searched_before_write), [true, false, true, false]);
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
        catalog_name: "Hammer Curl",
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
    _searched_before_write: false,
  });
  assert.deepEqual(result.details.json, { updated: true });
});

test("cardio schemas are present in every write detail and stay open", () => {
  const tools = collectTools();
  const fixture = JSON.parse(readFileSync(new URL("../../../../contracts/fuel_cardio_segments.v1.json", import.meta.url)));
  const details = [];
  for (const name of ["log_workout", "update_workout", "create_plan", "update_plan"]) {
    const tool = tools[`nbhd_fuel_${name}`];
    assert.match(tool.description, /PLANNED days: write "segments", never "exercises"/);
    assert.match(tool.description, /Effort is qualitative prescribed intensity/);
    const props = tool.parameters.properties;
    if (props.detail_json) details.push(props.detail_json);
    if (props.schedule_json) {
      details.push(props.schedule_json.additionalProperties.properties.detail_json);
      const override = props.week_overrides.additionalProperties.additionalProperties;
      assert.equal(override.type, undefined);
      assert.match(override.description, /null makes this a rest day/);
      details.push(override.properties.detail_json);
    }
  }
  assert.equal(details.length, 6);
  for (const detail of details) {
    assert.notEqual(detail.additionalProperties, false);
    assert.deepEqual(detail.properties.terrain.enum, fixture.terrains);
    const segments = detail.properties.segments;
    if (!segments.items?.oneOf) {
      assert.deepEqual(segments, { type: "array", items: { type: "object" }, description: "Cardio blocks — same shape as nbhd_fuel_create_plan detail_json.segments (server-validated)." });
      continue;
    }
    const variants = segments.items.oneOf;
    assert.deepEqual(variants.flatMap(v => v.properties.kind.enum).sort(), fixture.kinds.toSorted());
    assert.deepEqual(variants[1].properties.effort.enum, fixture.efforts);
    assert.deepEqual(variants[1].properties.recovery.properties.effort.enum, fixture.recovery_efforts);
    assert.equal(detail.properties.segments.minItems, 1);
    assert.equal(detail.properties.segments.maxItems, fixture.limits.blocks_max);
    assert.match(detail.properties.segments.description, /exactly one dose; repeat\/recovery only on interval; recovery needs repeat ≥ 2/);
    const assertProviderSafe = (node) => {
      if (!node || typeof node !== "object") return;
      for (const [key, value] of Object.entries(node)) {
        assert.ok(!["not", "if", "then"].includes(key), `unsupported cardio schema keyword: ${key}`);
        assertProviderSafe(value);
      }
    };
    assertProviderSafe(detail.properties.segments);
    for (const dose of [...variants, variants[1].properties.recovery]) {
      assert.deepEqual(dose.oneOf, [{ required: ["duration_s"] }, { required: ["distance_km"] }]);
    }
  }
  assert.equal(details.filter(detail => detail.properties.segments.items?.oneOf).length, 2);
  for (const name of ["log_workout", "update_workout", "create_plan", "update_plan"]) {
    const tool = tools[`nbhd_fuel_${name}`];
    assert.equal((JSON.stringify(tool).match(/Effort is qualitative prescribed intensity/g) || []).length, 1);
    const assertSafeShape = (node) => {
      if (!node || typeof node !== "object") return;
      assert.equal(Array.isArray(node.type), false);
      for (const [key, value] of Object.entries(node)) {
        assert.ok(!["not", "if", "then", "anyOf"].includes(key), `unsupported keyword ${key}`);
        assertSafeShape(value);
      }
    };
    assertSafeShape(tool.parameters);
  }
  const serializedSize = ["log_workout", "update_workout", "create_plan", "update_plan"]
    .reduce((size, name) => {
      const { name: toolName, description, parameters } = tools[`nbhd_fuel_${name}`];
      return size + JSON.stringify({ name: toolName, description, parameters }).length;
    }, 0);
  assert.ok(serializedSize < 29000, `Fuel write metadata grew to ${serializedSize}`);
  assert.equal(tools.nbhd_fuel_update_plan.parameters.properties.schedule_json.additionalProperties.required, undefined);
  assert.match(tools.nbhd_fuel_update_plan.description, /Do not target cardio days with accessory_rotations/);
});

test("create_plan passes cardio segments and extension fields through unchanged", async (t) => {
  setRuntimeEnv(t);
  const fixture = JSON.parse(readFileSync(new URL("../../../../contracts/fuel_cardio_segments.v1.json", import.meta.url)));
  const day = { category: "cardio", activity: "Intervals", detail_json: { ...fixture.examples.find(e => e.name === "intervals_mixed").detail_json, _normalized: [], extension: "kept" } };
  let body;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    body = JSON.parse(options.body);
    return response(201, JSON.stringify({ warnings: ["cardio days use segments, not exercises"] }));
  });
  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" }).nbhd_fuel_create_plan.execute("cardio", {
    name: "Runs", start_date: "2026-09-07", weeks: 2, days_per_week: 1, schedule_json: { monday: day },
  });
  assert.deepEqual(body.schedule_json.monday, day);
  assert.match(result.content[0].text, /cardio days use segments, not exercises/);
});

test("accessory rotation on a cardio day errors clearly without a write", async (t) => {
  setRuntimeEnv(t);
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; return response(200, "{}"); });
  const result = await collectTools({ apiBaseUrl: "https://nbhd.example" }).nbhd_fuel_create_plan.execute("cardio-rotation", {
    name: "Runs", start_date: "2026-09-07", weeks: 2, days_per_week: 1,
    schedule_json: { monday: { category: "cardio", activity: "Run", detail_json: { segments: [{ kind: "steady", duration_s: 600, effort: "easy" }] } } },
    accessory_rotations: [{ weekday: "monday", slot: { exercise_index: 0 }, every_weeks: 1, choices: [{ name: "Squat", sets: [] }, { name: "Lunge", sets: [] }] }],
  });
  assert.equal(calls, 0);
  assert.match(result.content[0].text, /has no exercises array/);
});
