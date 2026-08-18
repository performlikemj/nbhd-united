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

function toolsForContext(toolContext = { messageChannel: "ios" }) {
  const tools = new Map();
  register({
    pluginConfig: {},
    registerTool(definition) {
      const tool = typeof definition === "function" ? definition(toolContext) : definition;
      tools.set(tool.name, tool);
    },
  });
  return tools;
}

function expectedRecurrenceSchema() {
  const end = {
    oneOf: [
      {
        type: "object",
        additionalProperties: false,
        properties: {
          type: { const: "count" },
          count: { type: "integer", minimum: 2, maximum: 366 },
        },
        required: ["type", "count"],
      },
      {
        type: "object",
        additionalProperties: false,
        properties: {
          type: { const: "until" },
          date: { type: "string", pattern: "^\\d{4}-\\d{2}-\\d{2}$" },
        },
        required: ["type", "date"],
      },
      {
        type: "object",
        additionalProperties: false,
        properties: { type: { const: "never" } },
        required: ["type"],
      },
    ],
  };
  return {
    oneOf: [
      {
        type: "object",
        additionalProperties: false,
        properties: {
          freq: { const: "weekly" },
          interval: { type: "integer", minimum: 1, maximum: 99, default: 1 },
          weekdays: {
            type: "array",
            minItems: 1,
            maxItems: 7,
            uniqueItems: true,
            items: { type: "string", enum: ["mo", "tu", "we", "th", "fr", "sa", "su"] },
          },
          end,
        },
        required: ["freq", "end"],
      },
      {
        type: "object",
        additionalProperties: false,
        properties: {
          freq: { type: "string", enum: ["daily", "monthly", "yearly"] },
          interval: { type: "integer", minimum: 1, maximum: 99, default: 1 },
          end,
        },
        required: ["freq", "end"],
      },
    ],
  };
}

test("both create tools expose the exact recurrence contract and honesty rules", () => {
  const tools = toolsForContext();
  const recurrenceSentence =
    "Use recurrence only when the user asked for a repeating item or when proactively suggesting one and explicitly saying so.";
  const honestySentence =
    "You can NEVER modify or delete a calendar or reminder item after creation because no such tools exist; if asked to change or remove one, tell the user to do it in Apple Calendar/Reminders.";

  for (const name of ["nbhd_datebook_add_event", "nbhd_datebook_add_apple_reminder"]) {
    const tool = tools.get(name);
    assert.deepEqual(
      tool.parameters.properties.items.items.properties.recurrence,
      expectedRecurrenceSchema(),
    );
    assert.match(tool.description, new RegExp(recurrenceSentence.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.match(tool.description, new RegExp(honestySentence.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.doesNotMatch(tool.description, /Do not add[^.]*\brecurrence\b/i);
  }

  assert.match(
    tools.get("nbhd_datebook_add_event").description,
    /Do not add attendees, invitations, URLs, or alarms;/,
  );
});

test("both create tools forward recurrence without changing nested JSON", async () => {
  const cases = [
    [
      "nbhd_datebook_add_event",
      {
        title: "Team sync",
        time: {
          kind: "zoned",
          start_at: "2099-09-01T09:00:00+09:00",
          end_at: "2099-09-01T10:00:00+09:00",
          tz_id: "Asia/Tokyo",
        },
      },
    ],
    [
      "nbhd_datebook_add_apple_reminder",
      {
        title: "Water the plants",
        due: { kind: "all_day", date: "2099-09-01" },
      },
    ],
  ];

  for (const [name, baseItem] of cases) {
    let requestBody;
    globalThis.fetch = async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return new Response(JSON.stringify({
        state: "approval_pending",
        command_id: `command-${name}`,
        approval_surface: "app",
        delivery_state: "available",
      }), { status: 202 });
    };
    const recurrence = {
      freq: "weekly",
      interval: 2,
      weekdays: ["tu", "th"],
      end: { type: "until", date: "2099-12-31" },
    };
    const item = { ...baseItem, recurrence };
    const expectedItem = structuredClone(item);
    const tool = toolsForContext().get(name);

    await tool.execute(`call-${name}`, {
      items: [item],
      direct_user_originated: true,
    });

    assert.deepEqual(item, expectedItem);
    assert.deepEqual(requestBody.payload.items, [expectedItem]);
    assert.deepEqual(requestBody.payload.items[0].recurrence, recurrence);
    assert.equal(JSON.stringify(requestBody.payload.items[0].recurrence), JSON.stringify(recurrence));
  }
});
