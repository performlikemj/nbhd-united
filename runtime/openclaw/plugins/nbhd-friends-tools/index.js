import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-friends-tools" });

/**
 * NBHD Friends (Neighborhood) Tools Plugin
 *
 * Backstage-only. The agent can do exactly two things (design §5.3):
 *  - nbhd_propose_lesson_share: PROPOSE sharing an EXISTING star to a neighbor.
 *    Creates a proposal for the OWN human to approve — never publishes, never a
 *    grant. There is deliberately NO direct-post/publish tool.
 *  - nbhd_neighborhood_context: read the scrubbed sparks neighbors shared TO the
 *    tenant (the backstage absorb pull; also surfaced via the USER.md envelope).
 *
 * Mission tools land in PR6.
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

    const response = await fetch(url, { method, headers, body: requestBody, signal: controller.signal });
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

function tenantPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

// A friendship id is an opaque UUID; anything else is treated as an @handle.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default function register(api) {
  // The config generator emits `proposeEnabled` (an explicit boolean) for EVERY
  // friends-enabled tenant. The manifest configSchema MUST declare it or the
  // OpenClaw binary hard-rejects the whole plugin config at boot
  // (additionalProperties:false → "must not have additional properties") — that
  // is exactly the 2026-07-06 image-boot-smoke failure this file fixes. We read
  // it here and register the two PROPOSE tools ONLY when it is strictly true.
  // Fail-closed: absent/undefined/any non-true value → propose tools NOT
  // registered, matching the 403-backed design (the runtime endpoints deny
  // independently; this is the don't-even-offer-it layer).
  const cfg = (api.pluginConfig && typeof api.pluginConfig === "object") ? api.pluginConfig : {};
  const proposeEnabled = cfg.proposeEnabled === true;

  // ── Propose a share (proposal only — a human must approve) ───────────────
  if (proposeEnabled) {
    api.registerTool(wrap({
      name: "nbhd_propose_lesson_share",
      description:
        "PROPOSE sharing one of the user's EXISTING lessons/stars with a specific neighbor. This creates a PROPOSAL only — it is NOT shared until the user approves the scrubbed preview. NEVER tell the user something was shared unless an approval came back this turn. Do NOT propose health, money/finances, family/personal, or private-conversation content.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          lesson_id: {
            type: "integer",
            description: "The id of the user's existing lesson/star to propose sharing.",
          },
          why: {
            type: "string",
            description: "A short private note to yourself on why this would help — never shown to the neighbor.",
          },
          target: {
            type: "string",
            description: "The neighbor to propose sharing with: their @handle (e.g. 'kenji') or a friendship id.",
          },
        },
        required: ["lesson_id", "target"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const target = asTrimmedString(input.target).replace(/^@/, "");
        const body = { source_context: asTrimmedString(input.why) };
        if (UUID_RE.test(target)) {
          body.target_friendship_id = target;
        } else {
          body.target_handle = target;
        }
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/lessons/${encodeURIComponent(String(input.lesson_id))}/propose-share/`),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
  }

  // ── Neighborhood context (backstage absorb read) ─────────────────────────
  api.registerTool(wrap({
      name: "nbhd_neighborhood_context",
      description:
        "Read the scrubbed 'sparks' (lessons) your neighbors have shared with the user, plus the user's accepted neighbor handles. This is backstage context: hold it until useful and surface it naturally in conversation. You never post to a neighbor. Optionally pass the `since` cursor returned by a previous call to get only what's new.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          since: {
            type: "string",
            description: "Optional ISO-8601 cursor from a previous response's `cursor` field; returns only newer sparks.",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/neighborhood/context/"),
          method: "GET",
          query: { since: asTrimmedString(input.since) },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Mission context (the user's shared goals + crew progress) ────────────
  api.registerTool(wrap({
      name: "nbhd_mission_context",
      description:
        "Read the user's active Missions (shared goals with neighbors) and the crew's progress this week — per-member showed-up counts, streaks, each member's next step, and overall %. Use it to help YOUR human show up; you never act for another person and never message the group.",
      parameters: { type: "object", additionalProperties: false, properties: {} },
      async execute() {
        const payload = await callRuntime(api, { path: tenantPath(api, "/missions/"), method: "GET" });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Propose a Mission task (proposal only — the human approves) ───────────
  if (proposeEnabled) {
    api.registerTool(wrap({
      name: "nbhd_propose_mission_task",
      description:
        "PROPOSE ONE task for the USER toward a Mission they're part of. This creates a PROPOSAL only — it becomes the user's task only after they approve. You propose tasks for YOUR OWN human only, never for another member.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          mission_id: { type: "string", description: "The Mission id (from nbhd_mission_context)." },
          title: { type: "string", description: "A short, concrete task the user can do toward the goal." },
          description: { type: "string", description: "Optional extra detail." },
          due_date: { type: "string", description: "Optional YYYY-MM-DD due date." },
        },
        required: ["mission_id", "title"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/missions/${encodeURIComponent(asTrimmedString(input.mission_id))}/propose-task/`),
          method: "POST",
          body: {
            title: asTrimmedString(input.title),
            description: asTrimmedString(input.description),
            due_date: asTrimmedString(input.due_date),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
  }
}
