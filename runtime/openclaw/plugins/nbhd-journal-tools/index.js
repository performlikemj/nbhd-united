/**
 * NBHD Journal Tools Plugin
 *
 * Registers tools for the markdown-first collaborative journaling system:
 * - Daily notes: get, append, get raw markdown
 * - Long-term memory: get, update
 * - Journal context: combined endpoint for session init
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

function tenantPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

export default function register(api) {
  // ── Daily Note: Get ──────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_daily_note_get",
      description:
        "Get the raw markdown daily note for a specific date. Returns the full collaborative document (morning report, log entries, evening check-in).",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          date: {
            type: "string",
            description: "ISO date (YYYY-MM-DD). Defaults to today.",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/daily-note/"),
          method: "GET",
          query: { date: asTrimmedString(input.date) },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Daily Note: Append ───────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_daily_note_append",
      description:
        "Append a timestamped entry to the daily note. Use for logging agent actions, research findings, email summaries, or any noteworthy event. Auto-timestamps with current time and author=agent.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          content: {
            type: "string",
            description: "Markdown content to append as a new log entry.",
          },
          date: {
            type: "string",
            description:
              "ISO date (YYYY-MM-DD). Defaults to today. Use for backfilling.",
          },
        },
        required: ["content"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const content = asTrimmedString(input.content);
        if (!content) throw new Error("content is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/daily-note/append/"),
          method: "POST",
          body: {
            content,
            date: asTrimmedString(input.date) || undefined,
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Long-Term Memory: Get ────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_memory_get",
      description:
        "Get the user's long-term memory document (raw markdown). Contains curated preferences, goals, decisions, and lessons the agent has learned about the user over time.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/long-term-memory/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Long-Term Memory: Update ─────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_memory_update",
      description:
        "Replace the user's long-term memory document. Use after reviewing daily notes to curate preferences, goals, decisions, and lessons learned. Overwrites the entire document.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          markdown: {
            type: "string",
            description: "Full markdown content for the memory document.",
          },
        },
        required: ["markdown"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const markdown = asTrimmedString(input.markdown);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/long-term-memory/"),
          method: "PUT",
          body: { markdown },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Journal Context (Session Init) ───────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_journal_context",
      description:
        "Load recent daily notes and long-term memory in one call. Use at the start of every session to get caught up on the user's recent activity and what the agent knows about them.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          days: {
            type: "number",
            description: "Number of days of daily notes to fetch (default 7, max 30).",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/journal-context/"),
          method: "GET",
          query: {
            days: parseInteger(input.days, {
              defaultValue: 7,
              min: 1,
              max: 30,
            }),
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "nbhd_journal_evening_checkin",
      description:
        "Append evening check-in content to today's sectionized daily note.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          date: {
            type: "string",
            description: "Optional ISO date (YYYY-MM-DD). Defaults to today.",
          },
          content: {
            type: "string",
            description: "Raw markdown check-in content.",
          },
        },
        required: ["content"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const content = asTrimmedString(input.content);
        if (!content) {
          throw new Error("content is required");
        }
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/daily-note/append/"),
          method: "POST",
          body: {
            content,
            date: asTrimmedString(input.date) || undefined,
            section_slug: "evening-check-in",
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );
}
