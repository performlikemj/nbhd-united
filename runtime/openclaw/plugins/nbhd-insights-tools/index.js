import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-insights-tools" });

/**
 * NBHD Insights Tools Plugin
 *
 * Trajectory tools — let the assistant reason over the tenant's pillar
 * snapshots rather than only the current state. Phase 1 surfaces:
 *   - nbhd_insights_history  → list recent snapshots in a window
 *   - nbhd_insights_snapshot → drill into a specific snapshot
 *   - nbhd_insights_compare  → diff two snapshots
 *
 * Phase 1 supports pillar=gravity only. Other pillars 404 until their
 * snapshot pipelines ship.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 20000;
const ALLOWED_PILLARS = ["gravity"];
const ALLOWED_GRANULARITIES = ["daily", "weekly", "monthly"];

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

function buildUrl(baseUrl, path, query) {
  const url = new URL(`${baseUrl}${path}`);
  for (const [key, value] of Object.entries(query || {})) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  return url;
}

function renderPayload(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: { json: payload },
  };
}

async function callRuntime(api, { path, method = "GET", query, body }) {
  const runtime = getRuntimeConfig(api);
  const url = buildUrl(runtime.apiBaseUrl, path, query);
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

function insightsPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/insights/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

export default function register(api) {
  // ── History ─────────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_history",
      description:
        "List recent pillar snapshots (point-in-time state captures) over a time window. Use this to reason about the user's trajectory rather than only their current state — e.g. 'how has debt trended over the last 8 weeks?'. Returns snapshots newest-first with their full payloads. Phase 1 supports pillar='gravity' only.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Which pillar to query. Currently only 'gravity' (finance) is supported.",
          },
          window: {
            type: "string",
            description:
              "Time window to fetch (default '12w'). Format: <N><unit>, units: 'd'/'w'/'m'. Examples: '8w', '30d', '6m'.",
          },
          granularity: {
            type: "string",
            enum: ALLOWED_GRANULARITIES,
            description: "Snapshot cadence to filter on. Gravity defaults to 'weekly'.",
          },
        },
        required: ["pillar"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = { pillar: asTrimmedString(input.pillar) };
        const window = asTrimmedString(input.window);
        if (window) query.window = window;
        const granularity = asTrimmedString(input.granularity);
        if (granularity) query.granularity = granularity;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/history/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Snapshot detail ─────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_snapshot",
      description:
        "Fetch a single pillar snapshot by id, returning the full payload. Use after nbhd_insights_history identifies a period the user wants to dig into.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          snapshot_id: {
            type: "string",
            description: "UUID of the snapshot (from nbhd_insights_history).",
          },
        },
        required: ["snapshot_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const id = asTrimmedString(input.snapshot_id);
        if (!id) throw new Error("snapshot_id is required");
        const payload = await callRuntime(api, {
          path: insightsPath(api, `/snapshots/${encodeURIComponent(id)}/`),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Compare two periods ─────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_compare",
      description:
        "Compare two pillar snapshots. Returns both snapshots plus a signed 'totals_delta' (b minus a) for Gravity totals like debt, savings, minimum_payments. Use for 'what's changed between then and now?' questions.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Which pillar to compare. Currently only 'gravity' is supported.",
          },
          period_a: {
            type: "string",
            description: "Earlier snapshot id (UUID).",
          },
          period_b: {
            type: "string",
            description: "Later snapshot id (UUID). Deltas are computed as b - a.",
          },
        },
        required: ["pillar", "period_a", "period_b"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {
          pillar: asTrimmedString(input.pillar),
          period_a: asTrimmedString(input.period_a),
          period_b: asTrimmedString(input.period_b),
        };
        const payload = await callRuntime(api, {
          path: insightsPath(api, "/compare/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
