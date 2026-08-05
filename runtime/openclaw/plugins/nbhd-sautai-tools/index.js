import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-sautai-tools" });

/**
 * NBHD sautai Tools Plugin (Phase 0)
 *
 * Two tools, both reached through the NBHD Django runtime proxy — never sautai
 * directly (generation blocks 30-60s, past this plugin's 20s tool budget, and a
 * container-direct call would bypass the PII rehydrate chokepoint):
 *
 *   - nbhd_generate_meal_plan — confirm, then fire-and-forget. The first call
 *     returns the exact request and a short-lived token without creating a job.
 *     Only after the user verifies that preview does the same call with the token
 *     create a PENDING job for out-of-band QStash generation.
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

function asString(value) {
  return typeof value === "string" ? value : "";
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

function missingPlanDays(payload) {
  if (!Array.isArray(payload.missing_days)) return [];
  return payload.missing_days.map(asTrimmedString).filter(Boolean);
}

function isPartialPlan(payload) {
  return payload.complete === false || missingPlanDays(payload).length > 0;
}

function partialPlanGuidance(payload) {
  const missingDays = missingPlanDays(payload);
  const missingDetail = missingDays.length
    ? `These dates are missing: ${missingDays.join(", ")}.`
    : "sautai reports missing days, but did not provide their dates.";
  const progressDetail = payload.generation_in_progress
    ? " A plan update is in progress; do not say the missing days are filled until the updated plan arrives."
    : "";
  const cacheDetail = payload.cached
    ? " This is NBHD's cached copy and may also be slightly out of date."
    : "";
  return (
    `This meal plan is partial. Tell the user which days are missing: ${missingDetail} ` +
    `Never present this partial week as complete.${progressDetail}${cacheDetail}`
  );
}

// The proxy answers a missing SAUTAI_M2M_BASE_URL/SECRET with a 503 whose body
// carries error:"sautai_not_configured"; callRuntime surfaces both the code and
// its detail in the thrown message. Detect either so both tools can tell the
// user plainly instead of leaking a raw HTTP error.
function isNotConfigured(error) {
  const message = (error && error.message) || "";
  return message.includes("sautai_not_configured") || message.includes("not configured");
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
      // DRF commonly returns field errors at the top level, e.g.
      // {week_rating: ["..."]}, rather than under `detail`. Preserve that
      // compact validation payload so the model can correct and retry.
      const detail = compactErrorDetail(normalized);
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
        "Prepare, confirm, then generate a personalized weekly meal plan powered by sautai (the nutrition sibling of Fuel). TWO PHASES ARE MANDATORY. First call: omit confirm_token. The server returns a structured preview and does NOT create or send anything. Relay preview.confirmation_message to the user and wait for them to verify it. Second call, only after verification: pass the returned confirm_token with the identical preview.tool_parameters, including explicit week_start. Only then does asynchronous generation start. Never infer verification or silently skip the preview. sautai already stores the user's dietary profile, so do not ask for it before the preview. Pass user_prompt verbatim only for extra guidance the user gives this time. Use week='next' when the user says next/upcoming week, week='current' when they explicitly say this/current week, and otherwise omit both week and week_start so the server proposes a safe tenant-local week. Only pass week_start initially when the user names an explicit calendar date. regenerate=true remains destructive: use it only after explicit confirmation to replace an existing plan.",
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
          week: {
            type: "string",
            enum: ["current", "next"],
            description:
              "Optional target resolved in the user's timezone. Use 'next' for next/upcoming week or 'current' for an explicitly requested current/this week. If the user did not specify a week, omit this so the server proposes current week on Monday-Friday and next week on Saturday-Sunday.",
          },
          week_start: {
            type: "string",
            description:
              "Optional ISO calendar date (YYYY-MM-DD), only when the user explicitly names a date. It takes precedence over week and snaps backward to Monday. NEVER use this to compute next week; pass week='next' instead.",
          },
          number_of_days: {
            type: "integer",
            minimum: 1,
            maximum: 7,
            description: "Optional number of days to plan, 1-7. Omit for a full 7-day week.",
          },
          regenerate: {
            type: "boolean",
            description:
              "Set true ONLY after the user explicitly confirms rebuilding a week that already has a plan. This replaces that plan. New guidance for a week with no existing plan is NOT regeneration.",
          },
          confirm_replace: {
            type: "boolean",
            description:
              "Set true together with regenerate=true only after the user explicitly confirms replacing the existing plan after being shown or told about it. Never infer confirmation from new guidance alone.",
          },
          confirm_token: {
            type: "string",
            description:
              "Short-lived opaque token returned by the server preview. Omit on the first call. Pass it only after the user verifies preview.confirmation_message, together with the identical preview.tool_parameters and explicit week_start.",
          },
        },
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {};
          const userPrompt = asString(input.user_prompt);
          if (userPrompt) body.user_prompt = userPrompt;
          const inputWeek = asTrimmedString(input.week);
          if (inputWeek) body.week = inputWeek;
          const weekStart = asTrimmedString(input.week_start);
          if (weekStart) body.week_start = weekStart;
          if (input.number_of_days !== undefined && input.number_of_days !== null && input.number_of_days !== "") {
            body.number_of_days = parseInteger(input.number_of_days, { defaultValue: 7, min: 1, max: 7 });
          }
          if (input.regenerate === true) body.regenerate = true;
          if (input.confirm_replace === true) body.confirm_replace = true;
          const confirmToken = asTrimmedString(input.confirm_token);
          if (confirmToken) body.confirm_token = confirmToken;

          const payload = await callRuntime(api, {
            path: sautaiPath(api, "/sautai/generate-plan/"),
            body,
          });

          const week = asTrimmedString(payload.week_start);
          const weekPhrase = week ? ` for the week of ${week}` : "";

          if (payload.status === "confirmation_required") {
            const preview = asObject(payload.preview);
            const confirmationMessage =
              asString(preview.confirmation_message) ||
              "Review the structured meal-plan request in this result. Does it look correct to send?";
            return renderResult(
              `${confirmationMessage}\n\nDo NOT send or start generation yet. Wait for the user's explicit ` +
                "verification. Only then call nbhd_generate_meal_plan again with this result's confirm_token " +
                "and the identical preview.tool_parameters, including its explicit week_start.",
              payload,
            );
          }

          if (payload.status === "confirm_required") {
            return renderResult(
              `A meal plan already exists${weekPhrase}. Show or summarize the existing plan in this result and ` +
                "ask the user to explicitly confirm whether they want to replace it. Do NOT regenerate yet. Only " +
                "after they confirm, call nbhd_generate_meal_plan again with regenerate=true and confirm_replace=true.",
              payload,
            );
          }

          if (payload.status === "exists") {
            const guidance = userPrompt
              ? `A meal plan already exists${weekPhrase}, so the new guidance was NOT applied. Surface the existing ` +
                "plan in this result and offer to regenerate it. Replacing it requires explicit user confirmation; " +
                "only then call again with regenerate=true and confirm_replace=true."
              : `A meal plan already exists${weekPhrase}. Surface the existing plan in this result. Only offer to ` +
                "regenerate it if the user seems to want a new plan. Replacing it requires explicit user " +
                "confirmation; only then call again with regenerate=true and confirm_replace=true.";
            return renderResult(
              guidance,
              payload,
            );
          }

          if (payload.repairing_incomplete_plan === true) {
            const missingDays = missingPlanDays({ missing_days: payload.repairing_missing_days });
            const missingDetail = missingDays.length ? ` (${missingDays.join(", ")})` : "";
            return renderResult(
              `sautai is filling in the missing days${missingDetail}${weekPhrase}. Tell the user the existing ` +
                "meals will be left untouched and the repaired plan is on the way. Do NOT say the week is complete yet.",
              payload,
            );
          }

          // The proxy coalesced this onto an already-running generation that does
          // NOT include the new guidance (regenerate / this call's user_prompt).
          // Be honest: the guidance was NOT applied to what's cooking.
          if (payload.request_applied === false) {
            return renderResult(
              `A meal plan${weekPhrase} is ALREADY being generated, but WITHOUT this new guidance — it was ` +
                "requested moments ago and is still running. Tell the user their plan is already on the way, and " +
                "that once it arrives you can show it and ask whether they want it replaced with their guidance. " +
                "Do NOT claim their new guidance was applied, and do NOT say the plan is ready.",
              payload,
            );
          }

          return renderResult(
            `Meal-plan generation started${weekPhrase}, powered by sautai. It runs in the background and takes ` +
              "about 1–2 minutes; the user will get a notification when it's ready. Tell them you've started it " +
              "and it's on the way. You can call nbhd_get_meal_plan later to fetch it. Do NOT say the plan is " +
              "ready, and do NOT list or describe any meals — nothing has been generated yet.",
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
        "Read the user's weekly meal plan, powered by sautai (the nutrition sibling of Fuel). Fast synchronous read you MAY summarize to the user. Returns the plan's days and meals if one exists, or tells you none exists yet (then offer nbhd_generate_meal_plan). Use week='next' whenever the user says next week or the upcoming week. Only pass week_start when the user names an explicit calendar date; NEVER compute next week yourself via week_start. If the user says their plans stopped reflecting their sautai account or diet, tell them to reconnect sautai in Settings → Connected apps.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          week: {
            type: "string",
            enum: ["current", "next"],
            default: "current",
            description:
              "Target week resolved by the server in the user's timezone. Use 'next' whenever the user says next week or the upcoming week; otherwise use 'current'. NEVER compute next week via week_start.",
          },
          week_start: {
            type: "string",
            description:
              "Optional ISO calendar date (YYYY-MM-DD), only when the user explicitly names a date. It takes precedence over week and snaps backward to Monday. NEVER use this to compute next week.",
          },
        },
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {};
          const inputWeek = asTrimmedString(input.week);
          if (inputWeek) body.week = inputWeek;
          const weekStart = asTrimmedString(input.week_start);
          if (weekStart) body.week_start = weekStart;

          const payload = await callRuntime(api, {
            path: sautaiPath(api, "/sautai/current-plan/"),
            body,
          });

          if (isPartialPlan(payload)) {
            return renderResult(partialPlanGuidance(payload), payload);
          }

          if (payload.generation_in_progress) {
            const week = asTrimmedString(payload.generation_in_progress.week_start);
            const weekPhrase = week ? ` for the week of ${week}` : "";
            return renderResult(
              `Meal-plan generation${weekPhrase} is still running. Tell the user it usually takes about 1–2 ` +
                "minutes and they will get a notification when it is ready. You can call nbhd_get_meal_plan later " +
                "to fetch the completed plan. Do NOT say it is ready yet.",
              payload,
            );
          }

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
