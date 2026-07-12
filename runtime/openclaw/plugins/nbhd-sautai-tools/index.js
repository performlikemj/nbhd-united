import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-sautai-tools" });

/**
 * NBHD sautai Tools Plugin (Phase 0)
 *
 * Two tools, both reached through the NBHD Django runtime proxy — never sautai
 * directly (generation blocks 30-60s, past this plugin's 20s tool budget, and a
 * container-direct call would bypass the PII rehydrate chokepoint):
 *
 *   - nbhd_generate_meal_plan — fire-and-forget. The proxy creates a PENDING job
 *     and returns immediately; the plan is generated out of band via QStash and
 *     the user gets a push when it's ready. The tool RESPONSE tells the assistant
 *     honestly that it's on the way and NOT to claim the plan exists yet (the
 *     detailed latency messaging rides the response, not always-loaded AGENTS.md).
 *   - nbhd_get_meal_plan — fast synchronous read of the current plan. The proxy
 *     calls sautai's /current/ with a short timeout and falls back to NBHD's
 *     cache if sautai is slow.
 *
 * See docs/sautai-phase0-contract.md.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 20000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
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
    // Capped at 20s (not the 60/120s the sautai generation itself needs) —
    // this call only reaches the NBHD proxy endpoints, never sautai directly.
    // The generate proxy returns a fast ack; the current-plan proxy caps its
    // own sautai read below this. See the module docstring above.
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 20000,
  });

  if (!apiBaseUrl) throw new Error("NBHD_API_BASE_URL is required");
  if (!tenantId) throw new Error("NBHD_TENANT_ID is required");
  if (!internalKey) throw new Error("NBHD_INTERNAL_API_KEY is required");

  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

function renderText(text) {
  return { content: [{ type: "text", text: String(text) }] };
}

function renderPayload(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: { json: payload },
  };
}

function renderResult(text, payload) {
  // A human-readable instruction for the assistant PLUS the structured payload
  // in details — the response is the delivery vehicle for the usage rules.
  return { content: [{ type: "text", text: String(text) }], details: { json: payload } };
}

// The proxy answers a missing SAUTAI_M2M_BASE_URL/SECRET with a 503 whose body
// carries error:"sautai_not_configured"; callRuntime surfaces both the code and
// its detail in the thrown message. Detect either so both tools can tell the
// user plainly instead of leaking a raw HTTP error.
function isNotConfigured(error) {
  const message = (error && error.message) || "";
  return message.includes("sautai_not_configured") || message.includes("not configured");
}

async function callRuntime(api, { path, body }) {
  const runtime = getRuntimeConfig(api);
  const url = `${runtime.apiBaseUrl}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-NBHD-Internal-Key": runtime.internalKey,
        "X-NBHD-Tenant-Id": runtime.tenantId,
      },
      body: JSON.stringify(body || {}),
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
      throw new Error(`NBHD runtime error ${response.status}: ${code}${detailSuffix}`);
    }

    return asObject(payload);
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error(`NBHD runtime request timed out after ${runtime.requestTimeoutMs}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function sautaiPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

export default function register(api) {
  api.registerTool(
    wrap({
      name: "nbhd_generate_meal_plan",
      description:
        "Start generating a personalized weekly meal plan via sautai (the nutrition sibling of Fuel). ASYNC and fire-and-forget: it returns a job acknowledgment, not the plan — generation runs in the background. sautai already stores the user's dietary profile (allergies, preferences), so do not ask for those before calling. Pass user_prompt only for extra guidance the user gives this time (e.g. 'high protein, no pork').",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          user_prompt: {
            type: "string",
            maxLength: 2000,
            description:
              "Optional free-text guidance for this week's plan beyond the user's stored profile, e.g. 'high protein, no pork' or 'quick dinners only'. Omit if the user gave no specific guidance this time.",
          },
          week_start: {
            type: "string",
            description:
              "Optional ISO date (YYYY-MM-DD) for the Monday of the target week. Omit for the current week (resolved in the user's timezone).",
          },
          number_of_days: {
            type: "integer",
            minimum: 1,
            maximum: 7,
            description: "Optional number of days to plan, 1-7. Omit for a full 7-day week.",
          },
        },
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {};
          const userPrompt = asTrimmedString(input.user_prompt);
          if (userPrompt) body.user_prompt = userPrompt;
          const weekStart = asTrimmedString(input.week_start);
          if (weekStart) body.week_start = weekStart;
          if (input.number_of_days !== undefined && input.number_of_days !== null && input.number_of_days !== "") {
            body.number_of_days = parseInteger(input.number_of_days, { defaultValue: 7, min: 1, max: 7 });
          }

          const payload = await callRuntime(api, {
            path: sautaiPath(api, "/sautai/generate-plan/"),
            body,
          });

          const week = asTrimmedString(payload.week_start);
          const weekPhrase = week ? ` for the week of ${week}` : "";
          return renderResult(
            `Meal-plan generation started${weekPhrase}. It runs in the background and takes about a minute; ` +
              "the user will get a push notification when it's ready. Tell them you've started it and it's on the way. " +
              "Do NOT say the plan is ready, and do NOT list or describe any meals — nothing has been generated yet.",
            payload,
          );
        } catch (error) {
          if (isNotConfigured(error)) {
            return renderText(
              "sautai integration is not configured for this account — tell the user meal planning isn't set up yet.",
            );
          }
          return renderResult(`Couldn't start the meal plan: ${error.message}`, { error: error.message });
        }
      },
    }),
    { optional: true },
  );

  api.registerTool(
    wrap({
      name: "nbhd_get_meal_plan",
      description:
        "Read the user's current weekly meal plan from sautai (the nutrition sibling of Fuel). Fast synchronous read you MAY summarize to the user. Returns the plan's days and meals if one exists, or tells you none exists yet (then offer nbhd_generate_meal_plan). Optionally pass week_start (the Monday of a specific week); omit for the current week.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          week_start: {
            type: "string",
            description:
              "Optional ISO date (YYYY-MM-DD) for the Monday of the target week. Omit for the current week.",
          },
        },
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {};
          const weekStart = asTrimmedString(input.week_start);
          if (weekStart) body.week_start = weekStart;

          const payload = await callRuntime(api, {
            path: sautaiPath(api, "/sautai/current-plan/"),
            body,
          });

          if (payload.status === "no_plan") {
            const week = asTrimmedString(payload.week_start);
            const weekPhrase = week ? ` for the week of ${week}` : "";
            return renderResult(
              `No meal plan exists yet${weekPhrase}. Offer to generate one with nbhd_generate_meal_plan.`,
              payload,
            );
          }
          if (payload.cached) {
            return renderResult(
              "sautai was slow to respond, so this is the last plan NBHD cached for this week — it may be slightly out of date. You may summarize it, but say it's the cached copy.",
              payload,
            );
          }
          // A real, current plan — the assistant may summarize it freely.
          return renderPayload(payload);
        } catch (error) {
          if (isNotConfigured(error)) {
            return renderText(
              "sautai integration is not configured for this account — tell the user meal planning isn't set up yet.",
            );
          }
          return renderResult(`Couldn't read the meal plan: ${error.message}`, { error: error.message });
        }
      },
    }),
    { optional: true },
  );
}
