import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-sautai-tools" });

/**
 * NBHD sautai Tools Plugin (Phase 0)
 *
 * Kicks off an async weekly meal-plan generation via sautai's M2M API,
 * reached through the NBHD Django runtime proxy — never directly (the
 * generation itself takes 30-60s, well past this plugin's own tool-call
 * timeout, and a container-direct call would bypass the PII rehydrate
 * chokepoint). This call only creates a PENDING job and returns
 * immediately; the actual generation + "your plan is ready" push happen
 * out of band via QStash. See docs/sautai-phase0-contract.md.
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
    // this call only reaches the fast-ack proxy endpoint, never sautai
    // directly. See the module docstring above.
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 20000,
  });

  if (!apiBaseUrl) throw new Error("NBHD_API_BASE_URL is required");
  if (!tenantId) throw new Error("NBHD_TENANT_ID is required");
  if (!internalKey) throw new Error("NBHD_INTERNAL_API_KEY is required");

  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

function renderPayload(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: { json: payload },
  };
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
        "Start generating a personalized weekly meal plan via sautai (the nutrition sibling of Fuel). This is ASYNC — generation takes 30-60 seconds server-side, well past this call. It returns a job acknowledgment only. Tell the user you've started their meal plan and they'll get a notification when it's ready — NEVER say the plan is ready, list its meals, or describe its contents from this call's response. sautai already stores the user's dietary profile (allergies, preferences), so do not ask for those before calling; only call again for the SAME week if the user explicitly asks to regenerate.",
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
              "Optional ISO date (YYYY-MM-DD) for the Monday of the target week. Omit to let sautai default to the current week.",
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

          const payload = await callRuntime(api, {
            path: sautaiPath(api, "/sautai/generate-plan/"),
            body,
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );
}
