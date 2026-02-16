/**
 * NBHD Journal Tools Plugin (v2)
 *
 * Registers tools for the unified Document-based journaling system:
 * - Documents: get, update, append (works for any document kind)
 * - Daily notes: get, set section, append log entry
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
  // ── Document: Get ────────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_document_get",
      description:
        "Get a document by kind and slug. Works for any document type: daily notes, goals, tasks, ideas, projects, memory, weekly/monthly reviews. Returns the full markdown content.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            type: "string",
            description: "Document kind: daily, weekly, monthly, goal, project, tasks, ideas, memory.",
          },
          slug: {
            type: "string",
            description: "Document slug. For daily notes: YYYY-MM-DD. For singleton docs (tasks, ideas, memory): use the kind name. For projects: project-name.",
          },
        },
        required: ["kind"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/document/"),
          method: "GET",
          query: {
            kind: asTrimmedString(input.kind),
            slug: asTrimmedString(input.slug),
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Document: Create or Replace ──────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_document_put",
      description:
        "Create or replace a document. Use for writing full documents like goals, project notes, weekly reviews. For daily notes, prefer nbhd_daily_note_set_section or nbhd_daily_note_append.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            type: "string",
            description: "Document kind: daily, weekly, monthly, goal, project, tasks, ideas, memory.",
          },
          slug: {
            type: "string",
            description: "Document slug.",
          },
          title: {
            type: "string",
            description: "Document title (optional, auto-generated if not provided).",
          },
          markdown: {
            type: "string",
            description: "Full markdown content for the document.",
          },
        },
        required: ["kind", "markdown"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/document/"),
          method: "PUT",
          body: {
            kind: asTrimmedString(input.kind),
            slug: asTrimmedString(input.slug) || undefined,
            title: asTrimmedString(input.title) || undefined,
            markdown: asTrimmedString(input.markdown),
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Document: Append ─────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_document_append",
      description:
        "Append timestamped content to a document. Creates the document if it doesn't exist. Useful for adding entries to any document type.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            type: "string",
            description: "Document kind (default: daily).",
          },
          slug: {
            type: "string",
            description: "Document slug (default: today's date for daily).",
          },
          content: {
            type: "string",
            description: "Markdown content to append.",
          },
        },
        required: ["content"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const content = asTrimmedString(input.content);
        if (!content) throw new Error("content is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/document/append/"),
          method: "POST",
          body: {
            kind: asTrimmedString(input.kind) || "daily",
            slug: asTrimmedString(input.slug) || undefined,
            content,
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Daily Note: Get (legacy-compatible) ──────────────────────────────
  api.registerTool(
    {
      name: "nbhd_daily_note_get",
      description:
        "Get the daily note for a specific date. Returns the full collaborative document (morning report, log entries, evening check-in). Uses the legacy endpoint which also returns template sections.",
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

  // ── Daily Note: Set Section (legacy-compatible) ─────────────────────
  api.registerTool(
    {
      name: "nbhd_daily_note_set_section",
      description:
        "Set the content of a specific section in the daily note. Use for writing structured sections like Morning Report, Weather, News, Focus, or Evening Check-in.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          section_slug: {
            type: "string",
            description: "The slug of the section to set (e.g. 'morning-report', 'weather', 'news', 'focus', 'evening-check-in').",
          },
          content: {
            type: "string",
            description: "Full markdown content for the section. Overwrites existing section content.",
          },
          date: {
            type: "string",
            description: "ISO date (YYYY-MM-DD). Defaults to today.",
          },
        },
        required: ["section_slug", "content"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const sectionSlug = asTrimmedString(input.section_slug);
        const content = asTrimmedString(input.content);
        if (!sectionSlug) throw new Error("section_slug is required");
        if (!content) throw new Error("content is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/daily-note/append/"),
          method: "POST",
          body: {
            content,
            date: asTrimmedString(input.date) || undefined,
            section_slug: sectionSlug,
          },
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Daily Note: Append Log Entry (legacy-compatible) ─────────────────
  api.registerTool(
    {
      name: "nbhd_daily_note_append",
      description:
        "Append a quick timestamped log entry to the daily note. Auto-timestamps with current time and author=agent.",
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
            description: "ISO date (YYYY-MM-DD). Defaults to today.",
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
        "Get the user's long-term memory document (raw markdown). Contains curated preferences, goals, decisions, and lessons.",
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
        "Replace the user's long-term memory document. Use after reviewing daily notes to curate preferences, goals, decisions, and lessons learned.",
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
        "Load recent daily notes and long-term memory in one call. Use at the start of every session to get caught up.",
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

  // ── Evening Check-in (deprecated, kept for compat) ───────────────────
  api.registerTool(
    {
      name: "nbhd_journal_evening_checkin",
      description:
        "[DEPRECATED: Use nbhd_daily_note_set_section with section_slug='evening-check-in' instead.]",
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
        if (!content) throw new Error("content is required");
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
