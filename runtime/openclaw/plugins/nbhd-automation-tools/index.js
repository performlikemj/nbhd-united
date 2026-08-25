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

function renderCronCreatePayload(payload) {
  if (payload.state === "pending_approval") {
    return {
      content: [{
        type: "text",
        text: "This scheduled task is pending the user's approval and does not exist yet.",
      }],
      details: { json: payload },
    };
  }
  return renderPayload(payload);
}

function renderCronCreateConflict(error) {
  if (error && error.status === 409 && error.code === "request_id_conflict") {
    return {
      content: [{
        type: "text",
        text: "This scheduled-task request conflicts with an earlier request using the same request ID. Nothing new was created.",
      }],
      details: { error: error.code },
    };
  }
  if (error && error.status === 409 && error.code === "name_conflict") {
    return {
      content: [{
        type: "text",
        text: "A scheduled task with this name already exists. Nothing new was created.",
      }],
      details: { error: error.code },
    };
  }
  throw error;
}

function hiddenOrigin(input) {
  const origin = input && input._nbhd_origin;
  return origin !== null && typeof origin === "object" && !Array.isArray(origin) ? { origin } : {};
}

const TOOL_ERROR_DETAIL_MAX_CHARS = 2000;

function clampErrorDetail(text) {
  if (text.length <= TOOL_ERROR_DETAIL_MAX_CHARS) return text;
  return `${text.slice(0, TOOL_ERROR_DETAIL_MAX_CHARS)}… [truncated]`;
}

function compactErrorDetail(payload) {
  const normalized = asObject(payload);
  const entries = Object.entries(normalized).filter(([key]) => key !== "error");
  if (entries.length === 0) return "";

  const detail = normalized.detail;
  const detailIsOnlyKey = entries.length === 1 && detail !== undefined;
  if (detailIsOnlyKey && typeof detail === "string") {
    return detail.trim() ? clampErrorDetail(detail.trim()) : "";
  }

  const value = detailIsOnlyKey ? detail : Object.fromEntries(entries);
  if (value === null || (typeof value === "object" && Object.keys(value).length === 0)) return "";

  try {
    return clampErrorDetail(JSON.stringify(value));
  } catch {
    return clampErrorDetail(String(value));
  }
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
        payload = { detail: "upstream returned a non-JSON response body" };
      }
    }
    if (!response.ok) {
      const normalized = asObject(payload);
      const code = asTrimmedString(normalized.error) || "runtime_request_failed";
      // DRF commonly returns field errors at the top level, e.g.
      // {week_rating: ["..."]}, rather than under `detail`. Preserve that
      // compact validation payload so the model can correct and retry.
      const detail = compactErrorDetail(normalized);
      const detailSuffix = detail ? ` (${detail})` : "";
      const error = new Error(`NBHD runtime error ${response.status}: ${code}${detailSuffix}`);
      error.status = response.status;
      error.code = code;
      throw error;
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
        "Cron expression, EXACTLY 5 fields: minute hour day-of-month month day-of-week. " +
        "Seconds precision is not supported and a 6-field expression is rejected. " +
        "day-of-week is 0=Sunday, 1=Monday, ... 6=Saturday (7 also means Sunday); " +
        "names like MON,TUE,SUN and ranges like MON-FRI work too. Note this is NOT " +
        "the 0=Monday convention used by the fuel/workout tools. Required when " +
        "kind='cron'. Evaluated in the user's timezone (tz).",
    },
    tz: {
      type: "string",
      description:
        "IANA timezone for cron expressions, in Area/Location form (e.g. 'Asia/Tokyo'). " +
        "Optional — when omitted it defaults to the user's own timezone, which is " +
        "almost always what you want. Never use an 'Etc/*' name: their sign is " +
        "inverted ('Etc/GMT+9' is UTC MINUS 9), and they are rejected.",
    },
    at: {
      type: "string",
      description:
        "When to fire a one-shot. Required when kind='at'. Either a relative duration " +
        "— '20m', '2h', '1d', the easiest correct answer for 'remind me in N minutes' — " +
        "or an ISO-8601 timestamp WITH the user's timezone offset " +
        "(e.g. '2026-05-29T15:00:00+09:00'). A timestamp without an offset is rejected: " +
        "it would be read as UTC and fire hours away from what the user meant.",
    },
    everyMs: {
      type: "number",
      minimum: 60000,
      description:
        "Interval in MILLISECONDS for recurring 'every' schedules. Minimum 60000 " +
        "(one minute). Hourly = 3600000, every 15 minutes = 900000, daily = 86400000. " +
        "Sending seconds here (3600 for 'hourly') would fire every 3.6 seconds.",
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
        "May require approval; never claim the scheduled task exists while approval is pending. " +
        "Create a scheduled REMINDER that sends a fixed text to the user at the scheduled time. Use this when the user asks to be reminded of something simple with no live state lookup needed (e.g. 'remind me to take out the trash every Tuesday at 8am', 'remind me at 3pm tomorrow to call Mom'). The text you provide will be sent verbatim — write it in second person as if the user is reading it. If the user wants a summary of something that changes (their fuel progress, their open tasks, etc.) use nbhd_cron_create_domain_summary instead. If the user wants you to quote their own words back to them, use nbhd_cron_create_quote_user_intent. This tool sends chat pings; it is NOT the Apple Reminders app. For datebook-ready tenants, prefer nbhd_datebook_add_apple_reminder for 'remind me' asks. Use this tool only when the user explicitly requests an in-chat ping, nudge, or message, or when recurring scheduled check-in content is inherently conversational.",
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
      async execute(toolCallId, params) {
        const input = asObject(params);
        try {
          const payload = await callRuntime(api, {
            path: tenantPath(api, "/crons/pure_reminder/"),
            method: "POST",
            body: {
              name: asTrimmedString(input.name),
              schedule: asObject(input.schedule),
              text: typeof input.text === "string" ? input.text : "",
              cron_request_id: asTrimmedString(toolCallId),
              ...hiddenOrigin(input),
            },
          });
          return renderCronCreatePayload(payload);
        } catch (error) {
          return renderCronCreateConflict(error);
        }
      },
    }),
    { optional: true },
  );

  // ── quote_user_intent ─────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_cron_create_quote_user_intent",
      description:
        "May require approval; never claim the scheduled task exists while approval is pending. " +
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
              "nbhd_datebook_read",
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
      async execute(toolCallId, params) {
        const input = asObject(params);
        const body = {
          name: asTrimmedString(input.name),
          schedule: asObject(input.schedule),
          text: typeof input.text === "string" ? input.text : "",
          cron_request_id: asTrimmedString(toolCallId),
          ...hiddenOrigin(input),
        };
        const refresh = asTrimmedString(input.refresh_facts_via);
        if (refresh) body.refresh_facts_via = refresh;
        try {
          const payload = await callRuntime(api, {
            path: tenantPath(api, "/crons/quote_user_intent/"),
            method: "POST",
            body,
          });
          return renderCronCreatePayload(payload);
        } catch (error) {
          return renderCronCreateConflict(error);
        }
      },
    }),
    { optional: true },
  );

  // ── domain_summary ────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_cron_create_domain_summary",
      description:
        "May require approval; never claim the scheduled task exists while approval is pending. " +
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
              "nbhd_datebook_read",
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
              "The block type to render — MUST match the query_tool: nbhd_task_list→task_summary, nbhd_goal_list→goal_summary, nbhd_lessons_pending→lesson_summary, nbhd_journal_search→journal_summary, nbhd_calendar_list_events/nbhd_datebook_read→calendar_summary.",
          },
        },
        required: ["name", "schedule", "query_tool", "render_block"],
      },
      async execute(toolCallId, params) {
        const input = asObject(params);
        try {
          const payload = await callRuntime(api, {
            path: tenantPath(api, "/crons/domain_summary/"),
            method: "POST",
            body: {
              name: asTrimmedString(input.name),
              schedule: asObject(input.schedule),
              query_tool: asTrimmedString(input.query_tool),
              query_args: asObject(input.query_args),
              render_block: asTrimmedString(input.render_block),
              cron_request_id: asTrimmedString(toolCallId),
              ...hiddenOrigin(input),
            },
          });
          return renderCronCreatePayload(payload);
        } catch (error) {
          return renderCronCreateConflict(error);
        }
      },
    }),
    { optional: true },
  );
}
