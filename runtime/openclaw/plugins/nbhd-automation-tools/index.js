import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-automation-tools" });

/**
 * NBHD Automation Tools Plugin
 *
 * Exposes ONE typed cron-create tool per supported pattern. Each tool
 * has a concrete parameter schema for its pattern — no discriminated
 * unions — so the model can populate the right shape reliably.
 *
 * The agent should NOT have access to the raw `cron` tool once this
 * plugin is fleet-stable; that gate is removed via the tool policy
 * deny list in apps/orchestrator/tool_policy.py (Phase H per
 * CONTINUITY_cron-typed-patterns.md).
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 20000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") return defaultValue;
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) return defaultValue;
  return Math.max(min, Math.min(max, parsed));
}

function getRuntimeConfig(api) {
  const pluginConfig = asObject(api.pluginConfig);
  const apiBaseUrl = asTrimmedString(
    pluginConfig.apiBaseUrl || process.env.NBHD_API_BASE_URL,
  ).replace(/\/+$/, "");
  const tenantId = asTrimmedString(process.env.NBHD_TENANT_ID);
  const internalKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 60000,
  });

  if (!apiBaseUrl) throw new Error("NBHD_API_BASE_URL is required");
  if (!tenantId) throw new Error("NBHD_TENANT_ID is required");
  if (!internalKey) throw new Error("NBHD_INTERNAL_API_KEY is required");

  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

function buildUrl(baseUrl, path) {
  return new URL(`${baseUrl}${path}`);
}

function renderPayload(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: { json: payload },
  };
}

async function callRuntime(api, { path, method = "POST", body }) {
  const runtime = getRuntimeConfig(api);
  const url = buildUrl(runtime.apiBaseUrl, path);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const headers = {
      "X-NBHD-Internal-Key": runtime.internalKey,
      "X-NBHD-Tenant-Id": runtime.tenantId,
    };
    let requestBody;
    if (method !== "GET" && body !== undefined) {
      headers["Content-Type"] = "application/json";
      requestBody = JSON.stringify(body);
    }
    const response = await fetch(url, {
      method,
      headers,
      body: requestBody,
      signal: controller.signal,
    });
    const raw = await response.text();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = { raw };
      }
    }
    if (!response.ok) {
      const normalized = asObject(payload);
      const code = asTrimmedString(normalized.error) || "runtime_request_failed";
      const detail = asTrimmedString(normalized.detail);
      const detailSuffix = detail ? ` (${detail})` : "";
      throw new Error(
        `NBHD runtime error ${response.status}: ${code}${detailSuffix}`,
      );
    }
    return asObject(payload);
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error(
        `NBHD runtime request timed out after ${runtime.requestTimeoutMs}ms`,
      );
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function tenantPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

// ── Shared schema fragments ─────────────────────────────────────────────

// Schedule schema — accepted by every cron-create tool. The runtime
// endpoint does the full normalization; this schema enforces the surface
// shape so the model lands on a valid combination.
const SCHEDULE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    kind: {
      type: "string",
      enum: ["cron", "every", "at"],
      description:
        "cron: recurring on a cron expression. at: one-shot at an absolute time. every: recurring on a fixed interval.",
    },
    expr: {
      type: "string",
      description:
        "Cron expression (5 or 6 fields). Required when kind='cron'. Evaluate in the user's timezone (tz).",
    },
    tz: {
      type: "string",
      description:
        "IANA timezone for cron expressions (e.g. 'Asia/Tokyo'). Required when kind='cron' so the schedule fires in the user's local time.",
    },
    at: {
      type: "string",
      description:
        "ISO-8601 timestamp for one-shots. Required when kind='at'. Include the timezone offset (e.g. '2026-05-29T15:00:00+09:00').",
    },
    everyMs: {
      type: "number",
      description: "Interval in milliseconds for recurring 'every' schedules.",
    },
  },
  required: ["kind"],
};

const NAME_DESCRIPTION =
  "Short human-readable name for the cron, shown in the user's automations list. Must be unique per tenant. 3-80 characters.";

export default function register(api) {
  // ── pure_reminder ─────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_cron_create_pure_reminder",
      description:
        "Create a scheduled REMINDER that sends a fixed text to the user at the scheduled time. Use this when the user asks to be reminded of something simple with no live state lookup needed (e.g. 'remind me to take out the trash every Tuesday at 8am', 'remind me at 3pm tomorrow to call Mom'). The text you provide will be sent verbatim — write it in second person as if the user is reading it. If the user wants a summary of something that changes (their fuel progress, their open tasks, etc.) use nbhd_cron_create_domain_summary instead. If the user wants you to quote their own words back to them, use nbhd_cron_create_quote_user_intent.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          name: { type: "string", description: NAME_DESCRIPTION },
          schedule: SCHEDULE_SCHEMA,
          text: {
            type: "string",
            description:
              "The verbatim reminder text to send. Write in second person, mobile-readable, under ~200 characters when possible. 1-2000 chars.",
          },
        },
        required: ["name", "schedule", "text"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/crons/pure_reminder/"),
          method: "POST",
          body: {
            name: asTrimmedString(input.name),
            schedule: asObject(input.schedule),
            text: typeof input.text === "string" ? input.text : "",
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── quote_user_intent ─────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_cron_create_quote_user_intent",
      description:
        "Create a scheduled message that quotes the user's stored words back to them at the scheduled time. Use this when the user said something they want to be reminded of in their own words later (e.g. 'every Friday remind me about my cardiologist appointment Tuesday at 3pm'). Optionally specify refresh_facts_via to pull current calendar/tasks/etc. context at fire time so the assistant can frame the quote against today's state — but the user's verbatim words still appear in the outbound message.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          name: { type: "string", description: NAME_DESCRIPTION },
          schedule: SCHEDULE_SCHEMA,
          text: {
            type: "string",
            description:
              "The user's words to quote, captured as they said them. Will appear verbatim in the outbound message at fire time.",
          },
          refresh_facts_via: {
            type: "string",
            enum: [
              "nbhd_calendar_list_events",
              "nbhd_calendar_get_freebusy",
              "nbhd_gmail_list_messages",
              "nbhd_task_list",
              "nbhd_goal_list",
              "nbhd_daily_note_get",
            ],
            description:
              "OPTIONAL: a read-only tool to call before composing so the message can frame the quote against current state. Only specify if the user's text references something that changes over time (calendar appointments, recent emails, etc.). Omit for static reminders.",
          },
        },
        required: ["name", "schedule", "text"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const body = {
          name: asTrimmedString(input.name),
          schedule: asObject(input.schedule),
          text: typeof input.text === "string" ? input.text : "",
        };
        const refresh = asTrimmedString(input.refresh_facts_via);
        if (refresh) body.refresh_facts_via = refresh;
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/crons/quote_user_intent/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── domain_summary ────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_cron_create_domain_summary",
      description:
        "Create a scheduled summary of a specific domain's current state at fire time (tasks, goals, lessons, journal, calendar). Use this when the user wants a recurring rollup of state that changes over time (e.g. 'every Sunday show me my open tasks', 'every morning summarise my calendar for the day'). At fire time the assistant will call the query_tool first, then render the result. The query_tool must be from the supported set; render_block is the matching block type for that tool.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          name: { type: "string", description: NAME_DESCRIPTION },
          schedule: SCHEDULE_SCHEMA,
          query_tool: {
            type: "string",
            enum: [
              "nbhd_task_list",
              "nbhd_goal_list",
              "nbhd_lessons_pending",
              "nbhd_journal_search",
              "nbhd_calendar_list_events",
            ],
            description:
              "The read-only query to run at fire time. Choose by domain: tasks/goals/lessons/journal/calendar.",
          },
          query_args: {
            type: "object",
            additionalProperties: true,
            description:
              "Arguments to pass to the query tool. Tool-specific shape — see the individual tool's parameters. Example for nbhd_task_list: {status: 'open', pillar: 'gravity'}.",
          },
          render_block: {
            type: "string",
            enum: [
              "task_summary",
              "goal_summary",
              "lesson_summary",
              "journal_summary",
              "calendar_summary",
            ],
            description:
              "The block type to render — MUST match the query_tool: nbhd_task_list→task_summary, nbhd_goal_list→goal_summary, nbhd_lessons_pending→lesson_summary, nbhd_journal_search→journal_summary, nbhd_calendar_list_events→calendar_summary.",
          },
        },
        required: ["name", "schedule", "query_tool", "render_block"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/crons/domain_summary/"),
          method: "POST",
          body: {
            name: asTrimmedString(input.name),
            schedule: asObject(input.schedule),
            query_tool: asTrimmedString(input.query_tool),
            query_args: asObject(input.query_args),
            render_block: asTrimmedString(input.render_block),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
