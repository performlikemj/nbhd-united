import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-settings-tools" });

/**
 * NBHD Settings Tools Plugin
 *
 * Tools for the assistant to read or change tenant-level settings that
 * the consumer dashboard owns. Phase 1 exposes only primary-model
 * selection. The tier gate enforced by the consumer PreferredModelView
 * is reused at the runtime endpoint, so the assistant cannot quietly
 * upgrade itself past its tier ceiling — a forbidden model returns
 * "model_not_allowed" with the allowed list, which the assistant must
 * relay honestly to the user instead of hallucinating a switch.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 15000;

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
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 60000,
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

async function callRuntime(api, { path, method = "GET", body }) {
  const runtime = getRuntimeConfig(api);
  const url = new URL(`${runtime.apiBaseUrl}${path}`);
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

    // The 400 "model_not_allowed" path is expected — the assistant needs
    // to read the body to tell the user what's available. Surface it as
    // a normal return rather than throwing, so the tool result includes
    // the allowed_models array.
    if (response.status === 400 && asObject(payload).error === "model_not_allowed") {
      return asObject(payload);
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

function preferredModelPath(api) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/tenants/runtime/${encodeURIComponent(runtime.tenantId)}/preferred-model/`;
}

export default function register(api) {
  // ── Read state ──────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_get_preferred_model_state",
      description:
        "Read the user's current primary model and the list of models available to switch to. " +
        "Returns: preferred_model (current selection — empty string means tier default is in use), " +
        "applied_model (what the container is actually serving — may differ briefly during a switch), " +
        "model_tier, and allowed_models (array of {model_id, alias}). " +
        "Use this when the user asks 'what models can I use?' or before attempting nbhd_set_preferred_model.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute(_id, _params) {
        const payload = await callRuntime(api, {
          path: preferredModelPath(api),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Write — switch model ────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_set_preferred_model",
      description:
        "Switch the user's primary model. Pass model_id as one of the values returned by " +
        "nbhd_get_preferred_model_state. Pass an empty string to revert to the tier default. " +
        "If the model is not allowed on the user's tier, the response will have " +
        "error='model_not_allowed' with the allowed_models list — relay this honestly to the " +
        "user (e.g. 'Opus isn't on your tier, but you can switch to: <aliases>'). " +
        "After a successful switch, the new model is active within ~30s (config push + " +
        "gateway reload). 'applied_model' lags 'preferred_model' until the reload lands.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          model_id: {
            type: "string",
            description:
              "Exact model_id from allowed_models (not the alias). Empty string clears to tier default.",
          },
        },
        required: ["model_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        // Accept missing/null as empty (clear to default), but require the
        // caller to have considered the input — we don't infer.
        const modelId = typeof input.model_id === "string" ? input.model_id.trim() : "";
        const payload = await callRuntime(api, {
          path: preferredModelPath(api),
          method: "POST",
          body: { model_id: modelId },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
