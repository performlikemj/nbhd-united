import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-journal-tools" });

// Authoritative document kinds — mirrors apps/journal Document.Kind (the NBHD
// runtime validates `kind` against this set and 400s with `invalid_kind`
// otherwise; observed on the canary 2026-06-13 when the model passed an
// out-of-set kind). Used as a JSON-schema `enum` on every document `kind`
// param so the model is constrained to valid values rather than guessing.
// Keep in sync with apps/journal/models.py Document.Kind.
const DOCUMENT_KIND_ENUM = ["daily", "weekly", "monthly", "goal", "project", "tasks", "ideas", "memory"];
// Writable freeform kinds for nbhd_document_put. Excludes goal/tasks on purpose:
// those have dedicated lifecycle tools (nbhd_goal_* / nbhd_task_*) and new writes
// must land there, so the put enum and its "Do NOT use for goals or tasks"
// description agree instead of contradicting.
const DOCUMENT_PUT_KIND_ENUM = ["daily", "weekly", "monthly", "project", "ideas", "memory"];

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

async function callRuntime(api, { path, method = "GET", query, body, extraHeaders }) {
  const runtime = getRuntimeConfig(api);
  const url = buildUrl(runtime.apiBaseUrl, path, query);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const headers = {
      "X-NBHD-Internal-Key": runtime.internalKey,
      "X-NBHD-Tenant-Id": runtime.tenantId,
    };
    if (extraHeaders && typeof extraHeaders === "object") {
      for (const [k, v] of Object.entries(extraHeaders)) {
        if (typeof v === "string" && v.length > 0) headers[k] = v;
      }
    }
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

function journalPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/journal/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

export default function register(api) {
  // ── Document: Get ────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_document_get",
      description:
        "Get a document by kind and slug. Works for any document type: daily notes, goals, tasks, ideas, projects, memory, weekly/monthly reviews. Returns the full markdown content.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            type: "string",
            enum: DOCUMENT_KIND_ENUM,
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
    }),
    { optional: true },
  );

  // ── Document: Create or Replace ──────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_document_put",
      description:
        "Create or replace a free-form narrative document. Use for daily notes, weekly/monthly reviews, project narratives, ideas, long-term memory. Do NOT use for goals or tasks — those have dedicated lifecycle tools (nbhd_goal_* and nbhd_task_*) so their status, due dates, and completion are queryable instead of buried in markdown. Kinds: daily, weekly, monthly, project, ideas, memory.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            type: "string",
            enum: DOCUMENT_PUT_KIND_ENUM,
            description: "Document kind: daily, weekly, monthly, project, ideas, memory. (Goals and tasks use the dedicated nbhd_goal_* / nbhd_task_* tools, not this one.)",
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
    }),
    { optional: true },
  );

  // ── Weekly Review: Create (structured) ───────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_weekly_review_create",
      description:
        "Save a structured weekly review so it appears on the Horizons Weekly Pulse card. " +
        "Call this AFTER nbhd_document_put (which saves the free-form markdown) — both are required " +
        "to fully record a week: the document holds the narrative, this tool records the rating, " +
        "wins, challenges, lessons, and intentions in structured form.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          week_start: {
            type: "string",
            description: "Monday of the reviewed week (ISO date, YYYY-MM-DD).",
          },
          week_end: {
            type: "string",
            description: "Sunday of the reviewed week (ISO date, YYYY-MM-DD).",
          },
          week_rating: {
            type: "string",
            enum: ["thumbs-up", "thumbs-down", "meh"],
            description: "The user's overall rating of the week.",
          },
          mood_summary: {
            type: "string",
            description: "Brief summary of the week's mood/energy arc.",
          },
          top_wins: {
            type: "array",
            items: { type: "string" },
            description: "Biggest wins of the week (highlights first).",
          },
          top_challenges: {
            type: "array",
            items: { type: "string" },
            description: "Main challenges or difficulties.",
          },
          lessons: {
            type: "array",
            items: { type: "string" },
            description: "Lessons captured this week.",
          },
          intentions_next_week: {
            type: "array",
            items: { type: "string" },
            description: "Intentions for the upcoming week (1-3 items).",
          },
          raw_text: {
            type: "string",
            description: "Full free-form reflection text (can mirror the markdown body).",
          },
        },
        required: ["week_start", "week_end", "week_rating", "mood_summary", "raw_text"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const toStringList = (value) =>
          Array.isArray(value)
            ? value.map((item) => asTrimmedString(item)).filter((item) => item.length > 0)
            : [];
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/weekly-reviews/"),
          method: "POST",
          body: {
            week_start: asTrimmedString(input.week_start),
            week_end: asTrimmedString(input.week_end),
            week_rating: asTrimmedString(input.week_rating),
            mood_summary: asTrimmedString(input.mood_summary),
            raw_text: asTrimmedString(input.raw_text),
            top_wins: toStringList(input.top_wins),
            top_challenges: toStringList(input.top_challenges),
            lessons: toStringList(input.lessons),
            intentions_next_week: toStringList(input.intentions_next_week),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Document: Append ─────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_document_append",
      description:
        "Append timestamped content to a document. Creates the document if it doesn't exist. Useful for adding entries to any document type.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            type: "string",
            enum: DOCUMENT_KIND_ENUM,
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
    }),
    { optional: true },
  );

  // ── Daily Note: Get (legacy-compatible) ──────────────────────────────
  api.registerTool(wrap({
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
    }),
    { optional: true },
  );

  // ── Daily Note: Set Section (legacy-compatible) ─────────────────────
  api.registerTool(wrap({
      name: "nbhd_daily_note_set_section",
      description:
        "Set the content of a specific section in the daily note. REQUIRED: `section_slug` (the section to write, e.g. 'morning-report') AND `content` (the markdown). Both must be set in every call. Use for writing structured sections like Morning Report, Weather, News, Focus, or Evening Check-in.",
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
    }),
    { optional: true },
  );

  // ── Daily Note: Append Log Entry (legacy-compatible) ─────────────────
  api.registerTool(wrap({
      name: "nbhd_daily_note_append",
      description:
        "Append a quick timestamped log entry to the daily note. REQUIRED: `content` (the markdown to append) — must be set in every call. Auto-timestamps with current time and author=agent. Use ONLY for narrative reflection, mood, observations, or prose journaling. For ACTIONABLE ITEMS the user wants to remember to do (reminders, follow-ups, todos, 'remind me to X') use `nbhd_task_create` instead — even if mentioned casually in chat. Tasks have status + due_date and are queryable; daily-note prose is not.",
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
    }),
    { optional: true },
  );

  // ── Long-Term Memory: Get ────────────────────────────────────────────
  api.registerTool(wrap({
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
    }),
    { optional: true },
  );

  // ── Long-Term Memory: Update ─────────────────────────────────────────
  api.registerTool(wrap({
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
    }),
    { optional: true },
  );

  // ── Journal Context (Session Init) ───────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_journal_context",
      description:
        "Load recent daily notes, long-term memory, backbone goals/tasks, and recent constellation " +
        "activity (stars the user has been working through — their pinned notes, reflections, and " +
        "tutoring signals) in one call. Use at the start of every session to get caught up.",
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
    }),
    { optional: true },
  );

  // ── Evening Check-in (deprecated, kept for compat) ───────────────────
  api.registerTool(wrap({
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
    }),
    { optional: true },
  );

  // ── Journal Search ───────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_journal_search",
      description:
        "Search across all journal documents (daily notes, goals, projects, memory, reviews, etc.) by keyword or phrase. Uses full-text search. Use this to find past entries, recall what was written about a topic, or locate specific notes.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          query: {
            type: "string",
            description: "Search query (supports natural phrases and keywords).",
          },
          kind: {
            type: "string",
            enum: DOCUMENT_KIND_ENUM,
            description: "Optional: filter to a specific document kind (daily, weekly, monthly, goal, project, tasks, ideas, memory).",
          },
          limit: {
            type: "number",
            description: "Max results to return (default 10, max 50).",
          },
        },
        required: ["query"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = asTrimmedString(input.query);
        if (!query) throw new Error("query is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/journal/search/"),
          method: "GET",
          query: {
            q: query,
            kind: asTrimmedString(input.kind) || undefined,
            limit: parseInteger(input.limit, { defaultValue: 10, min: 1, max: 50 }),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Reconcile Scan (conversational gate function) ───────────────────
  api.registerTool(wrap({
      name: "nbhd_reconcile_scan",
      description:
        "Call this BEFORE replying on a conversational turn when the user just reported a concrete action that could change a goal, task, finance account, or fuel log — payments, transactions, workouts, body weight, task completion, goal progress, project status. Pass `claim` as a one-sentence summary of what they reported. Returns the active goals, open tasks, finance accounts, and fuel rows already filtered against the claim, each annotated with which typed write tool (`nbhd_goal_*`, `nbhd_task_*`, `nbhd_finance_*`, `nbhd_fuel_*`) to call to apply the update. Do NOT call this for questions, planning, venting, hypotheticals, or small talk.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          claim: {
            type: "string",
            description: "One-sentence summary of the concrete action the user just reported (e.g. 'paid credit card $400', 'did push workout today', 'lost 2 lbs').",
          },
          limit: {
            type: "number",
            description: "Max candidates to return (default 15, max 25).",
          },
        },
        required: ["claim"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const claim = asTrimmedString(input.claim);
        if (!claim) throw new Error("claim is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/reconcile/scan/"),
          method: "GET",
          query: {
            claim,
            limit: parseInteger(input.limit, { defaultValue: 15, min: 1, max: 25 }),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Lessons: suggest/search/pending ─────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_lesson_suggest",
      description:
        "Suggest a candidate lesson for user approval. REQUIRED: `text` (the lesson/insight in 1-3 sentences) — must be set in every call. Creates a pending lesson with user-facing text, optional context, source metadata, and auto-generated tags.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          text: {
            type: "string",
            description: "The lesson/insight in 1-3 sentences.",
          },
          context: {
            type: "string",
            description: "Optional context where the lesson came from.",
          },
          source_type: {
            type: "string",
            description: "Optional provenance type (conversation, journal, reflection, article, experience).",
          },
          source_ref: {
            type: "string",
            description: "Optional provenance reference (date, URL, message id, etc.).",
          },
          tags: {
            type: "array",
            description: "Optional tags for auto-categorization.",
            items: {
              type: "string",
            },
          },
        },
        required: ["text"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const text = asTrimmedString(input.text);
        if (!text) throw new Error("text is required");

        let tags = [];
        if (Array.isArray(input.tags)) {
          tags = input.tags
            .map((item) => asTrimmedString(item))
            .filter((item) => item.length > 0);
        }

        const payload = await callRuntime(api, {
          path: tenantPath(api, "/lessons/"),
          method: "POST",
          body: {
            text,
            context: asTrimmedString(input.context) || undefined,
            source_type: asTrimmedString(input.source_type) || undefined,
            source_ref: asTrimmedString(input.source_ref) || undefined,
            tags,
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Lessons: text similarity search ────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_lesson_search",
      description:
        "Search lessons by text or semantic similarity. Use for recall during conversation and review workflows.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          query: {
            type: "string",
            description: "Search query or idea summary to find similar lessons.",
          },
          limit: {
            type: "number",
            description: "Max results to return (default 10, max 50).",
          },
        },
        required: ["query"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = asTrimmedString(input.query);
        if (!query) throw new Error("query is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/lessons/search/"),
          method: "GET",
          query: {
            q: query,
            limit: parseInteger(input.limit, { defaultValue: 10, min: 1, max: 50 }),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Lessons: pending queue ────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_lessons_pending",
      description:
        "Get the current count and list of pending lessons waiting for user approval.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/lessons/pending/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Constellation: enriched notes (pinned notes, reflections, tutoring) ─
  api.registerTool(wrap({
      name: "nbhd_constellation_notes",
      description:
        "Read the enriched context behind the user's constellation stars (lessons): their pinned " +
        "galaxy notes, the reflections they journaled on a star, and the honest signals the assistant " +
        "captured while tutoring them on it (did they restate it accurately, find edge cases, make " +
        "connections, reach mastery). Use this to teach to how THIS person actually thinks — their " +
        "strengths and blind spots on topics they've worked through. With no arguments it returns the " +
        "stars they've been most active on lately; pass `q` to search by topic, or `star_id` to drill " +
        "into one star.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          q: {
            type: "string",
            description: "Optional topic/idea to search stars by (semantic + text).",
          },
          star_id: {
            type: "number",
            description: "Optional id of a single star to fetch full context for.",
          },
          limit: {
            type: "number",
            description: "Max stars to return (default 5, max 25).",
          },
          days: {
            type: "number",
            description: "Lookback window for 'recently active' stars (default 30, max 365).",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {
          limit: parseInteger(input.limit, { defaultValue: 5, min: 1, max: 25 }),
          days: parseInteger(input.days, { defaultValue: 30, min: 1, max: 365 }),
        };
        const q = asTrimmedString(input.q);
        if (q) query.q = q;
        if (input.star_id !== undefined && input.star_id !== null && input.star_id !== "") {
          query.star_id = parseInteger(input.star_id, { defaultValue: 0, min: 0, max: 2147483647 });
        }
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/constellation/notes/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Sessions: undistilled YardTalk pushes ─────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_sessions_pending",
      description:
        "List YardTalk work sessions that have not yet been distilled into the journal/tasks/goals/memory primitives. " +
        "For each returned session, decide where its content belongs and write it using existing tools: " +
        "`nbhd_daily_note_append` for the work log of the session date; " +
        "`nbhd_document_put` / `nbhd_document_append` for tasks (kind='tasks'), goals (kind='goal'), ideas, or per-project notes (kind='project'); " +
        "`nbhd_memory_update` for cross-session context worth carrying forward. " +
        "Then call `nbhd_session_mark_processed` once per session with a brief record of what you wrote. " +
        "Skip a session (call mark with `skip_reason`) if it's a stub under ~30s, has no actionable content, or is purely a duplicate of something already filed. " +
        "Returns ordered by session_start desc, default limit 10, max 25.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          limit: {
            type: "number",
            description: "Max sessions to return (default 10, max 25).",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/sessions/pending/"),
          method: "GET",
          query: {
            limit: parseInteger(input.limit, { defaultValue: 10, min: 1, max: 25 }),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Sessions: mark distilled ──────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_session_mark_processed",
      description:
        "Mark a YardTalk session as distilled, recording what you wrote (or why you skipped). " +
        "Call this AFTER you've written the session's content into journal/tasks/goals/memory via the appropriate write tools. " +
        "The processed_summary is free-form but should record what you actually did, e.g. " +
        "`{daily_note_date: '2026-05-08', tasks_added: ['<task ids>'], goals_referenced: ['<goal ids>'], memory_updated: true, notes: 'short rationale'}`. " +
        "If skipping, set `{skipped: true, skip_reason: '<reason>'}`. " +
        "Idempotent — calling on an already-processed session returns the existing record without overwriting.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          session_id: {
            type: "string",
            description: "The session id (UUID) returned by nbhd_sessions_pending.",
          },
          processed_summary: {
            type: "object",
            description:
              "Free-form record of what you wrote during distillation, or why you skipped. Stored as JSON.",
          },
        },
        required: ["session_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const sessionId = asTrimmedString(input.session_id);
        if (!sessionId) throw new Error("session_id is required");
        const summary = asObject(input.processed_summary);
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/sessions/${encodeURIComponent(sessionId)}/mark-processed/`),
          method: "POST",
          body: { processed_summary: summary },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Platform Issue Report ────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_platform_issue_report",
      description:
        "Silently report a platform issue (missing capability, tool error, config problem) for operator troubleshooting. NEVER mention these issues to the user. Use this tool, then gracefully degrade or skip the affected feature.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          category: {
            type: "string",
            enum: ["missing_capability", "tool_error", "config_issue", "rate_limit", "auth_error", "other"],
            description: "Issue category.",
          },
          severity: {
            type: "string",
            enum: ["low", "medium", "high", "critical"],
            description: "Issue severity. low=minor inconvenience, medium=feature degraded, high=feature broken, critical=core functionality down.",
          },
          tool_name: {
            type: "string",
            description: "Name of the tool or capability that failed (e.g. 'web_search', 'nbhd_daily_note_append').",
          },
          summary: {
            type: "string",
            description: "Brief description of the issue. DO NOT include any user message content or personal information.",
          },
          detail: {
            type: "string",
            description: "Additional technical context. DO NOT include user PII, messages, or sensitive data.",
          },
        },
        required: ["category", "summary"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const summary = asTrimmedString(input.summary);
        if (!summary) throw new Error("summary is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/platform-issue/report/"),
          method: "POST",
          body: {
            category: asTrimmedString(input.category) || "other",
            severity: asTrimmedString(input.severity) || "low",
            tool_name: asTrimmedString(input.tool_name),
            summary,
            detail: asTrimmedString(input.detail),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Send message to user (for cron jobs / proactive messages) ──────
  api.registerTool(wrap({
    name: "nbhd_send_to_user",
    description:
      "Send a message to the user via Telegram or LINE. Use this in cron " +
      "sessions or whenever you need to proactively reach out. Do NOT use " +
      "during normal conversation — just reply directly instead. When " +
      "running inside a cron job, pass `job_name` (find it in the prompt " +
      "preamble) so the user's next inbound reply correctly threads back " +
      "to this message.",
    parameters: {
      type: "object",
      required: ["message"],
      properties: {
        message: {
          type: "string",
          description: "The message text to send. Supports Markdown formatting.",
        },
        job_name: {
          type: "string",
          description:
            "Optional. The cron job name from the prompt preamble (e.g. " +
            "'Morning Briefing', 'Evening Check-in'). Used by Django to " +
            "tag the outbound for thread-continuity context on the user's " +
            "next reply. Safe to omit for ad-hoc proactive sends.",
        },
      },
    },
    async execute(_id, params) {
      const input = asObject(params);
      const message = asTrimmedString(input.message);
      if (!message) throw new Error("message is required");
      const jobName = asTrimmedString(input.job_name);
      const payload = await callRuntime(api, {
        path: tenantPath(api, "/send-to-user/"),
        method: "POST",
        body: { message },
        extraHeaders: jobName ? { "X-NBHD-Job-Name": jobName.slice(0, 64) } : undefined,
      });
      return renderPayload(payload);
    },
  }));

  // ── Cron Phase 2 sync summary ─────────────────────────────────────
  // Final step for any foreground scheduled task that messaged the user.
  // Calling this tool delegates the entire sync-cron creation to Django:
  // the agent provides only a 2-3 sentence summary; Django composes the
  // cron expression, sessionTarget, payload, and self-removal text.
  //
  // If the run did NOT send the user a message, do not call this tool —
  // absence of call = no sync needed.
  api.registerTool(wrap({
    name: "nbhd_cron_phase2_summary",
    description:
      "FINAL STEP for scheduled tasks (cron jobs): if you sent the user a " +
      "message during this run via nbhd_send_to_user, call this tool with " +
      "a 2-3 sentence summary of what happened so the user's main chat " +
      "session has context when they reply. Do NOT call this if the run " +
      "was silent (no user message sent). Backend handles all sync-cron " +
      "creation — you only provide the summary.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["summary", "job_name"],
      properties: {
        summary: {
          type: "string",
          description:
            "2-3 sentence summary, written for the main chat session's context. " +
            "Mention what sections you wrote and what you sent the user.",
        },
        job_name: {
          type: "string",
          description:
            "The name of the cron job that ran (e.g. 'Evening Check-in', " +
            "'Morning Briefing'). Find this in the prompt preamble.",
        },
      },
    },
    async execute(_id, params) {
      const input = asObject(params);
      const summary = asTrimmedString(input.summary);
      const jobName = asTrimmedString(input.job_name);
      if (!summary) throw new Error("summary is required");
      if (!jobName) throw new Error("job_name is required");
      const payload = await callRuntime(api, {
        path: tenantPath(api, "/cron-phase2-summary/"),
        method: "POST",
        body: { summary, job_name: jobName },
      });
      return renderPayload(payload);
    },
  }));

  // ── Update user profile (timezone, display_name, language) ─────────
  api.registerTool(wrap({
    name: "nbhd_update_profile",
    description:
      "Update the user's profile settings. Use ONLY after the user has " +
      "explicitly confirmed the change in conversation. Supported fields: " +
      "timezone (IANA string like 'America/New_York'), display_name, language, " +
      "location_city, location_lat, location_lon. " +
      "When updating timezone, all scheduled tasks are automatically synced. " +
      "When updating location, weather forecasts will use those coordinates.",
    parameters: {
      type: "object",
      properties: {
        timezone: {
          type: "string",
          description:
            "IANA timezone string, e.g. 'America/New_York', 'Asia/Tokyo', 'Europe/London'.",
        },
        display_name: {
          type: "string",
          description: "The user's preferred display name.",
        },
        language: {
          type: "string",
          description: "ISO language code, e.g. 'en', 'ja', 'es'.",
        },
        location_city: {
          type: "string",
          description: "City name, e.g. 'Osaka', 'Brooklyn', 'London'.",
        },
        location_lat: {
          type: "number",
          description: "Latitude (-90 to 90).",
        },
        location_lon: {
          type: "number",
          description: "Longitude (-180 to 180).",
        },
      },
    },
    async execute(_id, params) {
      const input = asObject(params);
      const body = {};
      if (input.timezone) body.timezone = asTrimmedString(input.timezone);
      if (input.display_name) body.display_name = asTrimmedString(input.display_name);
      if (input.language) body.language = asTrimmedString(input.language);
      if (input.location_city) body.location_city = asTrimmedString(input.location_city);
      if (input.location_lat !== undefined && input.location_lon !== undefined) {
        body.location_lat = Number(input.location_lat);
        body.location_lon = Number(input.location_lon);
      }
      if (Object.keys(body).length === 0) {
        throw new Error("At least one field (timezone, display_name, language, location_city, location_lat, location_lon) is required");
      }
      const payload = await callRuntime(api, {
        path: tenantPath(api, "/profile/"),
        method: "PATCH",
        body,
      });
      return renderPayload(payload);
    },
  }));

  // ── Workspace: List ──────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_workspace_list",
      description:
        "List the user's workspaces. Workspaces are a content-organization label only — they do NOT route chat messages or create separate conversation contexts. Returns name, slug, description, is_default, last_used_at for each. Only call when the user explicitly asks to see their workspaces.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute(_id, _params) {
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/workspaces/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Workspace: Create ────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_workspace_create",
      description:
        "Create a new workspace label. Use ONLY when the user explicitly asks ('create a workspace for X'). Workspaces are a content-organization label — they do NOT route chat messages, so do not proactively create one because you detected a recurring topic. Maximum 4 workspaces per user.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          name: {
            type: "string",
            description:
              "Short workspace name (max 60 chars). E.g. 'Work', 'Translation', 'Fitness'.",
          },
          description: {
            type: "string",
            description:
              "What topics this workspace label covers. Free-form note — not used for routing.",
          },
        },
        required: ["name"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const name = asTrimmedString(input.name);
        if (!name) throw new Error("name is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/workspaces/"),
          method: "POST",
          body: {
            name,
            description: asTrimmedString(input.description),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Workspace: Update ────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_workspace_update",
      description:
        "Update a workspace label's name or description. Workspaces no longer route chat messages — description changes are free-form notes only. Use when the user explicitly asks to refine a workspace label.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          slug: {
            type: "string",
            description: "The workspace slug to update.",
          },
          name: {
            type: "string",
            description: "New name (optional, max 60 chars).",
          },
          description: {
            type: "string",
            description: "New description (optional). Empty string clears it.",
          },
        },
        required: ["slug"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const slug = asTrimmedString(input.slug);
        if (!slug) throw new Error("slug is required");
        const body = {};
        if (input.name !== undefined) body.name = asTrimmedString(input.name);
        if (input.description !== undefined) body.description = asTrimmedString(input.description);
        if (Object.keys(body).length === 0) {
          throw new Error("At least one of name or description is required");
        }
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/workspaces/${encodeURIComponent(slug)}/`),
          method: "PATCH",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Workspace: Delete ────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_workspace_delete",
      description:
        "Delete a workspace label. The default workspace cannot be deleted. Workspaces no longer route chat messages — deletion only removes the label, not any conversation history. Always confirm with the user before deleting.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          slug: {
            type: "string",
            description: "The workspace slug to delete.",
          },
        },
        required: ["slug"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const slug = asTrimmedString(input.slug);
        if (!slug) throw new Error("slug is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/workspaces/${encodeURIComponent(slug)}/`),
          method: "DELETE",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // nbhd_workspace_switch was removed 2026-05-20 along with workspace
  // chat routing — see docs/implementation/remove-workspace-chat-routing.md.
  // Workspaces remain as a dormant content-organization primitive accessible
  // via nbhd_workspace_list / _create / _update / _delete above, but no
  // longer steer chat sessions.

  // ── Typed Goal lifecycle ─────────────────────────────────────────────
  // Replaces Document(kind="goal") for tenants with experimental_typed_journal_lifecycle.
  // Goals have state (active/achieved/abandoned/expired). Status changes are
  // DATABASE UPDATES, not prose edits — so completed goals don't linger in
  // context as if they were still active.

  const PILLAR_ENUM = ["gravity", "fuel", "core", "lessons", "journal", "constellation"];

  api.registerTool(wrap({
      name: "nbhd_goal_create",
      description:
        "Create a new goal — a durable intention with a target outcome. Use for anything the user wants to achieve. Do NOT use nbhd_document_put with kind='goal' (deprecated). Examples of GOOD goal titles: 'Achieve debt-free status on student loans', 'Build a daily journaling habit'. Examples of BAD goal titles (these are tasks, not goals): 'Pay April loan payment', 'Buy groceries'.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          title: { type: "string", description: "Short goal description." },
          description: { type: "string", description: "Optional longer narrative." },
          pillar: { type: "string", enum: PILLAR_ENUM, description: "Pillar this goal belongs to (optional)." },
          target: { type: "object", description: "Optional structured target — shape is free, e.g. {kind: 'numeric', value: 40000, unit: 'usd'}." },
          target_date: { type: "string", description: "ISO date YYYY-MM-DD (optional)." },
          parent_goal_id: { type: "string", description: "Parent Goal UUID for sub-goals (optional)." },
        },
        required: ["title"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/goals/"),
          method: "POST",
          body: {
            title: asTrimmedString(input.title),
            description: asTrimmedString(input.description) || undefined,
            pillar: asTrimmedString(input.pillar) || undefined,
            target: input.target || undefined,
            target_date: asTrimmedString(input.target_date) || undefined,
            parent_goal_id: asTrimmedString(input.parent_goal_id) || undefined,
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_goal_update",
      description:
        "Update a goal — change title, description, target, target_date, pillar, or parent. Use PATCH semantics (only included fields are updated). For status changes, prefer nbhd_goal_achieve or nbhd_goal_abandon.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          goal_id: { type: "string", description: "Goal UUID." },
          title: { type: "string" },
          description: { type: "string" },
          pillar: { type: "string", enum: PILLAR_ENUM },
          target: { type: "object" },
          target_date: { type: "string" },
          parent_goal_id: { type: "string" },
        },
        required: ["goal_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const goalId = asTrimmedString(input.goal_id);
        if (!goalId) throw new Error("goal_id is required");
        const body = {};
        for (const k of ["title", "description", "pillar", "target_date", "parent_goal_id"]) {
          const v = asTrimmedString(input[k]);
          if (v) body[k] = v;
        }
        if (input.target !== undefined) body.target = input.target;
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/goals/${encodeURIComponent(goalId)}/`),
          method: "PATCH",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_goal_achieve",
      description:
        "Mark a goal as achieved. Sets status=achieved and achieved_at=now. Use whenever the user confirms or you observe a goal has been reached.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          goal_id: { type: "string", description: "Goal UUID." },
        },
        required: ["goal_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const goalId = asTrimmedString(input.goal_id);
        if (!goalId) throw new Error("goal_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/goals/${encodeURIComponent(goalId)}/achieve/`),
          method: "POST",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_goal_abandon",
      description:
        "Mark a goal as abandoned. Use when the user has decided not to pursue it further. Does NOT delete the goal — preserved for history.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          goal_id: { type: "string", description: "Goal UUID." },
        },
        required: ["goal_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const goalId = asTrimmedString(input.goal_id);
        if (!goalId) throw new Error("goal_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/goals/${encodeURIComponent(goalId)}/abandon/`),
          method: "POST",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_goal_list",
      description:
        "List goals. Filter by status (active/achieved/abandoned/expired), pillar, or parent_goal_id. Default returns all goals for the tenant. Use this BEFORE stating any goal-related fact — long-term memory should not contain goal lists.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          status: { type: "string", enum: ["active", "achieved", "abandoned", "expired"] },
          pillar: { type: "string", enum: PILLAR_ENUM },
          parent_goal_id: { type: "string" },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {};
        for (const k of ["status", "pillar", "parent_goal_id"]) {
          const v = asTrimmedString(input[k]);
          if (v) query[k] = v;
        }
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/goals/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_goal_get",
      description: "Fetch a single goal by ID with full details.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          goal_id: { type: "string", description: "Goal UUID." },
        },
        required: ["goal_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const goalId = asTrimmedString(input.goal_id);
        if (!goalId) throw new Error("goal_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/goals/${encodeURIComponent(goalId)}/`),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Typed Task lifecycle ─────────────────────────────────────────────
  // Replaces Document(kind="tasks") markdown bullets. One row per task; status
  // changes are database UPDATES, so a completed task drops out of "open" queries
  // immediately and no stale "- [ ] X" line lingers in agent context.

  api.registerTool(wrap({
      name: "nbhd_task_create",
      description:
        "PREFERRED tool for ANY actionable item the user mentions wanting to do — reminders, follow-ups, todos, 'remind me to X', 'I should Y', 'don't forget Z' — even when mentioned casually in chat. Captures intent as a queryable database row with status (open/in_progress/done/skipped/deferred) and due_date instead of as prose buried in a daily note. ALWAYS prefer this over `nbhd_daily_note_append` for items the user might want to come back to. Do NOT use `nbhd_document_put` with kind='tasks' (deprecated). If the task pertains to a specific object in another pillar (e.g. paying a particular loan tracked in Gravity), pass `related_ref` so the task points to the source-of-truth row. Do NOT record CURRENT VALUES in the title or description (no balances, no totals, no '$X owed') — values live in their tracking systems and should be queried fresh.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          title: { type: "string", description: "Short task description." },
          description: { type: "string", description: "Optional longer context." },
          pillar: { type: "string", enum: PILLAR_ENUM, description: "Pillar this task belongs to (optional)." },
          due_date: { type: "string", description: "ISO date YYYY-MM-DD (optional)." },
          parent_goal_id: { type: "string", description: "Goal UUID to attach to (optional)." },
          related_ref: {
            type: "object",
            description:
              "Pointer to a specific tracked object: {pillar: 'gravity', object_type: 'FinanceAccount', object_id: '<uuid>'}.",
          },
        },
        required: ["title"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/tasks/"),
          method: "POST",
          body: {
            title: asTrimmedString(input.title),
            description: asTrimmedString(input.description) || undefined,
            pillar: asTrimmedString(input.pillar) || undefined,
            due_date: asTrimmedString(input.due_date) || undefined,
            parent_goal_id: asTrimmedString(input.parent_goal_id) || undefined,
            related_ref: input.related_ref || undefined,
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_task_update",
      description:
        "Update a task — change title, description, pillar, due_date, parent goal, or related_ref. REQUIRED: `task_id` (the task UUID to update) — must be set in every call. PATCH semantics. For status changes, prefer nbhd_task_complete / nbhd_task_skip / nbhd_task_defer.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          task_id: { type: "string", description: "Task UUID." },
          title: { type: "string" },
          description: { type: "string" },
          pillar: { type: "string", enum: PILLAR_ENUM },
          due_date: { type: "string" },
          parent_goal_id: { type: "string" },
          related_ref: { type: "object" },
        },
        required: ["task_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const taskId = asTrimmedString(input.task_id);
        if (!taskId) throw new Error("task_id is required");
        const body = {};
        for (const k of ["title", "description", "pillar", "due_date", "parent_goal_id"]) {
          const v = asTrimmedString(input[k]);
          if (v) body[k] = v;
        }
        if (input.related_ref !== undefined) body.related_ref = input.related_ref;
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/tasks/${encodeURIComponent(taskId)}/`),
          method: "PATCH",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_task_complete",
      description:
        "Mark a task as done. Sets status=done and completed_at=now. Use whenever the user confirms or you observe completion. Do NOT instead add 'verified ✅' to a note — that creates stale prose; this updates the source of truth so future queries return correct state.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          task_id: { type: "string", description: "Task UUID." },
        },
        required: ["task_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const taskId = asTrimmedString(input.task_id);
        if (!taskId) throw new Error("task_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/tasks/${encodeURIComponent(taskId)}/complete/`),
          method: "POST",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_task_skip",
      description: "Mark a task as skipped. Use when the user decided not to do it.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          task_id: { type: "string", description: "Task UUID." },
        },
        required: ["task_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const taskId = asTrimmedString(input.task_id);
        if (!taskId) throw new Error("task_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/tasks/${encodeURIComponent(taskId)}/skip/`),
          method: "POST",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_task_defer",
      description: "Mark a task as deferred. Use when the user is postponing it.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          task_id: { type: "string", description: "Task UUID." },
        },
        required: ["task_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const taskId = asTrimmedString(input.task_id);
        if (!taskId) throw new Error("task_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/tasks/${encodeURIComponent(taskId)}/defer/`),
          method: "POST",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_task_list",
      description:
        "List tasks. Filter by status (open/in_progress/done/skipped/deferred), pillar, parent_goal_id, due_before, due_after. Use this BEFORE stating any task status — never rely on memory for what's open vs done. Example: 'any open finance tasks?' → nbhd_task_list({status: 'open', pillar: 'gravity'}).",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          status: { type: "string", enum: ["open", "in_progress", "done", "skipped", "deferred"] },
          pillar: { type: "string", enum: PILLAR_ENUM },
          parent_goal_id: { type: "string" },
          due_before: { type: "string", description: "ISO date YYYY-MM-DD." },
          due_after: { type: "string", description: "ISO date YYYY-MM-DD." },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {};
        for (const k of ["status", "pillar", "parent_goal_id", "due_before", "due_after"]) {
          const v = asTrimmedString(input[k]);
          if (v) query[k] = v;
        }
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/tasks/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_current_status",
      description:
        "Authoritative as-of-now snapshot of the user's current state: open " +
        "tasks, active goals, and finance payment obligations (per-cycle " +
        "paid/partial/unpaid). Derived live from the typed task/goal store and " +
        "the finance ledger — NOT from the daily note, USER.md, or memory. Call " +
        "this FIRST in any scheduled/proactive turn and ground the message on " +
        "it: never raise, nudge, or re-ask about a task or obligation that is " +
        "not reported here as open/active/unpaid. Items absent here are " +
        "done/closed — do not resurface them. When finance is paused the " +
        "snapshot omits obligations entirely.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/current-status/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  api.registerTool(wrap({
      name: "nbhd_task_get",
      description: "Fetch a single task by ID with full details.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          task_id: { type: "string", description: "Task UUID." },
        },
        required: ["task_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const taskId = asTrimmedString(input.task_id);
        if (!taskId) throw new Error("task_id is required");
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/tasks/${encodeURIComponent(taskId)}/`),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Parameterized Journal query ──────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_journal_query",
      description:
        "Query the journal (entries, tasks, goals) by structured filters. " +
        "USE THIS for any quantitative or list-shaped journal claim — task counts by status, " +
        "open tasks for a goal, entries in a date range, overdue items. Never recite a count or " +
        "list from memory; if you don't see the rows in this turn, query for them.\n\n" +
        "Parameters:\n" +
        "  • resource (required): one of \"entries\", \"tasks\", \"goals\".\n" +
        "  • window: time range. Shape: {\"kind\": <enum>, \"value\": <if needed>}.\n" +
        "      Enum: today | yesterday | tomorrow | all | last_n_days (+value: int 1-730) | next_n_days |\n" +
        "      last_n_weeks (+value: int 1-104) | last_n_months (+value: int 1-24) | this_week | last_week |\n" +
        "      month_to_date | last_month | year_to_date | last_year | since (+value: \"YYYY-MM-DD\") |\n" +
        "      between (+value: [\"YYYY-MM-DD\", \"YYYY-MM-DD\"]).\n" +
        "  • window_field: which date column the window resolves against. Per-resource options:\n" +
        "      entries:  \"date\" (default) | \"created_at\"\n" +
        "      tasks:    \"due_date\" (default) | \"created_at\" | \"updated_at\" | \"completed_at\"\n" +
        "      goals:    \"target_date\" (default) | \"created_at\" | \"updated_at\" | \"achieved_at\"\n" +
        "  • filter: dict, resource-specific:\n" +
        "      entries: {mood?: str (fuzzy), energy?: \"low\"|\"medium\"|\"high\"}\n" +
        "      tasks:   {status?: \"open\"|\"in_progress\"|\"done\"|\"skipped\"|\"deferred\", pillar?: str,\n" +
        "                parent_goal_id?: uuid, has_due_date?: bool, overdue?: bool}\n" +
        "      goals:   {status?: \"active\"|\"achieved\"|\"abandoned\"|\"expired\", pillar?: str,\n" +
        "                parent_goal_id?: uuid, has_target_date?: bool}\n" +
        "  • fields: optional list of field names; id is always included. Omit for all fields.\n" +
        "  • aggregate: \"count\" only — journal rows have no numeric columns to sum/avg.\n" +
        "  • group_by: optional. entries: \"energy\"|\"mood\". tasks/goals: \"status\"|\"pillar\".\n" +
        "  • order_by: optional. Prefix with \"-\" for descending. Defaults: entries \"-date\", tasks \"status,due_date\", goals \"status,target_date\".\n" +
        "  • limit: optional. Default 50, max 500. meta.has_more is true if cap was reached.\n\n" +
        "Returns: {\"data\": [...], \"meta\": {schema_version, computed_at, tenant_tz, as_of, window_resolved_to: {from,to}, row_count, has_more, query_hash}}\n\n" +
        "Examples:\n" +
        "  Open tasks due this week:\n" +
        "    {\"resource\": \"tasks\", \"window\": {\"kind\": \"this_week\"}, \"filter\": {\"status\": \"open\"}}\n" +
        "  How many tasks closed last month:\n" +
        "    {\"resource\": \"tasks\", \"window\": {\"kind\": \"last_month\"}, \"window_field\": \"completed_at\", \"filter\": {\"status\": \"done\"}, \"aggregate\": \"count\"}\n" +
        "  Overdue items right now:\n" +
        "    {\"resource\": \"tasks\", \"filter\": {\"overdue\": true}}\n" +
        "  Active goals for the lessons pillar:\n" +
        "    {\"resource\": \"goals\", \"filter\": {\"status\": \"active\", \"pillar\": \"lessons\"}}\n" +
        "  Mood frequency in the last 30 days:\n" +
        "    {\"resource\": \"entries\", \"window\": {\"kind\": \"last_n_days\", \"value\": 30}, \"aggregate\": \"count\", \"group_by\": \"mood\"}\n\n" +
        "GROUNDING CONTRACT: Any journal count or list you state to the user MUST come from a query " +
        "result returned in this turn. Don't infer. Don't recall. If row_count is 0, say so plainly.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          resource: { type: "string", enum: ["entries", "tasks", "goals"] },
          window: {
            type: "object",
            additionalProperties: false,
            properties: {
              kind: {
                type: "string",
                enum: [
                  "today", "yesterday", "tomorrow", "all",
                  "last_n_days", "next_n_days", "last_n_weeks", "last_n_months",
                  "this_week", "last_week", "month_to_date", "last_month",
                  "year_to_date", "last_year", "since", "between",
                ],
              },
              value: {},
            },
            required: ["kind"],
          },
          window_field: { type: "string" },
          filter: { type: "object", additionalProperties: true },
          fields: { type: "array", items: { type: "string" } },
          aggregate: { type: "string", enum: ["count", "sum", "avg", "min", "max"] },
          aggregate_field: { type: "string" },
          group_by: { type: "string" },
          order_by: { type: "string" },
          limit: { type: "integer", minimum: 1, maximum: 500 },
        },
        required: ["resource"],
      },
      async execute(_id, params) {
        const payload = await callRuntime(api, {
          path: journalPath(api, "/query/"),
          method: "POST",
          body: asObject(params),
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
