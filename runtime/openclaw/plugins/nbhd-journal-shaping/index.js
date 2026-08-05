import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-journal-shaping" });

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

export default function register(api) {
  const cfg = api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
  if (cfg.journalShapingEnabled !== true) return;

  api.registerTool(
    wrap({
      name: "nbhd_journal_template_get",
      description:
        "Read the current default daily-note template before proposing any journal reshape. Returns the template name and complete sections list; always get first because update replaces the whole list.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/journal/template/"),
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(
    wrap({
      name: "nbhd_journal_template_update",
      description:
        "After reading the current template, showing the exact replacement sections, and getting explicit user agreement, replace the default daily-note template's complete sections list. This changes future notes only and never modifies existing journal notes.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          sections: {
            type: "array",
            description: "The complete replacement list, not a partial patch.",
            maxItems: 12,
            items: {
              type: "object",
              additionalProperties: false,
              properties: {
                slug: { type: "string", maxLength: 64 },
                title: { type: "string", maxLength: 120 },
                content: { type: "string", maxLength: 4000 },
                source: {
                  type: "string",
                  enum: ["agent", "human", "shared"],
                },
              },
              required: ["slug", "title", "content", "source"],
            },
          },
        },
        required: ["sections"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/journal/template/update/"),
          method: "POST",
          body: { sections: input.sections },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
