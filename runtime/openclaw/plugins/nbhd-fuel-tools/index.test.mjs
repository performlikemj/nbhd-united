import test from "node:test";
import assert from "node:assert/strict";

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
