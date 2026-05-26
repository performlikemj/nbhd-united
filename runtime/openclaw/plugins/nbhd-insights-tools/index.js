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

  // ── Baseline ────────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_baseline",
      description:
        "Rolling baseline stats (mean, stdev, latest, latest_z, trend, sample_size, freshness_days) for a topic over a window. Use this BEFORE deciding if a pattern is anomalous. A high |latest_z| (>~1.5) hints anomaly, but always weigh against context — a known event (wedding, bonus) can produce a high z without being a real pattern. Returns supported=false when the topic isn't currently extractable from snapshot payloads; fall back to nbhd_insights_history.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Which pillar (currently only 'gravity').",
          },
          topic: {
            type: "string",
            description: "Topic slug, e.g. 'debt', 'savings', 'minimum_payments'.",
          },
          window_weeks: {
            type: "integer",
            minimum: 1,
            maximum: 104,
            description: "Trailing window in weeks (default 12).",
          },
          granularity: {
            type: "string",
            enum: ALLOWED_GRANULARITIES,
            description: "Snapshot cadence (default 'weekly').",
          },
        },
        required: ["pillar", "topic"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {
          pillar: asTrimmedString(input.pillar),
          topic: asTrimmedString(input.topic),
        };
        if (input.window_weeks !== undefined) query.window_weeks = input.window_weeks;
        const granularity = asTrimmedString(input.granularity);
        if (granularity) query.granularity = granularity;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/baseline/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Insight list (your existing memory) ─────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_list",
      description:
        "List AssistantInsight rows you've previously recorded for this user — your own memory. Filter by pillar, topic, and/or status (open|confirmed|refuted|expired). ALWAYS check this before raising a new observation, so you don't repeat a refuted one or re-raise something already confirmed. Newest first.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Filter to a pillar (omit for all).",
          },
          topic: {
            type: "string",
            description: "Filter to a topic slug (omit for all).",
          },
          status: {
            type: "string",
            enum: ["open", "confirmed", "refuted", "expired"],
            description: "Filter by lifecycle status (omit for all).",
          },
          limit: {
            type: "integer",
            minimum: 1,
            maximum: 100,
            description: "Max rows (default 20).",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {};
        const pillar = asTrimmedString(input.pillar);
        if (pillar) query.pillar = pillar;
        const topic = asTrimmedString(input.topic);
        if (topic) query.topic = topic;
        const statusArg = asTrimmedString(input.status);
        if (statusArg) query.status = statusArg;
        if (input.limit !== undefined) query.limit = input.limit;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/insights/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Record (write a new open insight) ───────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_record",
      description:
        "Record an observation you've just raised with the user — your interpretation of a pattern, not a raw number. Status starts as 'open' until the user confirms or refutes. Use `evidence_refs` to point to the specific snapshots/window that support the claim (e.g. {snapshot_ids: [...], window: '8w'}). The `topic` accepts either a canonical slug or a natural string; if it's new, the registry creates a 'proposed' topic that ops can later promote. Skip noise — single-week blips, <10% baseline deltas, things the user already explicitly mentioned.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Which pillar.",
          },
          topic: {
            type: "string",
            description: "Canonical slug ('dining') OR a natural string ('weekend takeout') that will be auto-resolved or proposed.",
          },
          statement: {
            type: "string",
            description: "Your phrased observation, in the same voice you used with the user. e.g. 'Dining ran 1.8x your usual the last 3 weeks'.",
          },
          evidence_refs: {
            type: "object",
            description: "Pointers to the data that supports this claim. Free-form JSON object, but conventionally {snapshot_ids: [...], window: '8w'}.",
          },
          confidence: {
            type: "number",
            minimum: 0,
            maximum: 1,
            description: "Optional 0..1. Default 0 — Phase 3's confidence engine will compute properly; passing your own value here is hint-only.",
          },
        },
        required: ["pillar", "topic", "statement"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const body = {
          pillar: asTrimmedString(input.pillar),
          topic: asTrimmedString(input.topic),
          statement: asTrimmedString(input.statement),
        };
        if (input.evidence_refs !== undefined) body.evidence_refs = input.evidence_refs;
        if (input.confidence !== undefined) body.confidence = input.confidence;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/insights/record/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Confirm ─────────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_confirm",
      description:
        "Mark an existing insight as confirmed by the user. Call this when the user agrees with an observation you raised. Idempotent — re-confirms just append to the response history.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          insight_id: {
            type: "string",
            description: "UUID of the insight to confirm.",
          },
          note: {
            type: "string",
            description: "Optional short context from the user's reply (e.g. 'wedding season').",
          },
        },
        required: ["insight_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const id = asTrimmedString(input.insight_id);
        if (!id) throw new Error("insight_id is required");
        const body = {};
        const note = asTrimmedString(input.note);
        if (note) body.note = note;
        const payload = await callRuntime(api, {
          path: insightsPath(api, `/insights/${encodeURIComponent(id)}/confirm/`),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Refute ──────────────────────────────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_insights_refute",
      description:
        "Mark an existing insight as refuted — the user corrected you. The row stays on record so you remember being wrong (and don't re-raise the same thing). Be quick to refute; refusing to admit wrong is the failure mode.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          insight_id: {
            type: "string",
            description: "UUID of the insight to refute.",
          },
          note: {
            type: "string",
            description: "Optional short context (e.g. 'that was a one-off, helped a friend with rent').",
          },
        },
        required: ["insight_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const id = asTrimmedString(input.insight_id);
        if (!id) throw new Error("insight_id is required");
        const body = {};
        const note = asTrimmedString(input.note);
        if (note) body.note = note;
        const payload = await callRuntime(api, {
          path: insightsPath(api, `/insights/${encodeURIComponent(id)}/refute/`),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Phase 3: graduated voice ────────────────────────────────────────

  // Signals — structured breakdown the LLM judges register from
  api.registerTool(
    wrap({
      name: "nbhd_insights_signals",
      description:
        "Get structured signals for a topic — the inputs you use to JUDGE which voice register to use this turn. Returns data state (sample_size, latest_z, trend, freshness), calibration counts (confirmed/refuted/open), intent (has_stated_goal + summary), user_voice_pref (their explicit override if any), and hard_floors (mechanical safety rails you cannot exceed). YOU pick the register — observation / hypothesis / soft prescription / direct — by weighing these signals against the LIVE CONVERSATION CONTEXT (user mood, regime changes they mentioned, seasonal cues). Never go hotter than hard_floors permit. Honor any non-zero register_offset. Skip-noise rules from observation mode still apply.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Which pillar.",
          },
          topic: {
            type: "string",
            description: "Topic slug, e.g. 'debt', 'savings', 'dining'.",
          },
          window_weeks: {
            type: "integer",
            minimum: 1,
            maximum: 104,
            description: "Trailing window for the data signals (default 12).",
          },
          granularity: {
            type: "string",
            enum: ALLOWED_GRANULARITIES,
            description: "Snapshot cadence (default 'weekly').",
          },
        },
        required: ["pillar", "topic"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {
          pillar: asTrimmedString(input.pillar),
          topic: asTrimmedString(input.topic),
        };
        if (input.window_weeks !== undefined) query.window_weeks = input.window_weeks;
        const granularity = asTrimmedString(input.granularity);
        if (granularity) query.granularity = granularity;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/signals/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // Voice pref — persist user-explicit overrides
  api.registerTool(
    wrap({
      name: "nbhd_insights_voice_pref_set",
      description:
        "Persist the user's EXPLICIT voice override for a (pillar, topic). Call this when the user says something like 'just tell me about dining' (register_offset=+1), 'be more cautious on debt' (register_offset=-1), or 'go back to default for dining' (register_offset=0). NEVER call this on your own inference — only when the user explicitly says they want you hotter/cooler. Omit `topic` to set a pillar-wide override. Idempotent upsert.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Which pillar.",
          },
          topic: {
            type: "string",
            description: "Topic slug (omit for a pillar-wide override).",
          },
          register_offset: {
            type: "integer",
            enum: [-1, 0, 1],
            description:
              "-1 = drop one register (more cautious). 0 = clear override. +1 = bump one register (more direct).",
          },
          tone: {
            type: "string",
            enum: ["gentle", "direct"],
            description: "Optional tone preference. Default: gentle.",
          },
          volume: {
            type: "string",
            enum: ["silent", "weekly", "live"],
            description: "Optional proactive-volume preference. Default: weekly.",
          },
        },
        required: ["pillar", "register_offset"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const body = {
          pillar: asTrimmedString(input.pillar),
          register_offset: input.register_offset,
        };
        const topic = asTrimmedString(input.topic);
        if (topic) body.topic = topic;
        const tone = asTrimmedString(input.tone);
        if (tone) body.tone = tone;
        const volume = asTrimmedString(input.volume);
        if (volume) body.volume = volume;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/voice-prefs/set/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // Voice pref — list the user's current overrides
  api.registerTool(
    wrap({
      name: "nbhd_insights_voice_pref_list",
      description:
        "List the user's current voice-pref overrides. Useful when the user says 'what register are you using on X?' or 'show me my settings'. Filter by pillar and/or topic.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          pillar: {
            type: "string",
            enum: ALLOWED_PILLARS,
            description: "Filter by pillar (omit for all).",
          },
          topic: {
            type: "string",
            description: "Filter by topic slug (omit for all).",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {};
        const pillar = asTrimmedString(input.pillar);
        if (pillar) query.pillar = pillar;
        const topic = asTrimmedString(input.topic);
        if (topic) query.topic = topic;

        const payload = await callRuntime(api, {
          path: insightsPath(api, "/voice-prefs/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Yesterday's signals — cross-pillar day-scoped roll-up ───────────
  //
  // Distinct from nbhd_insights_history / baseline (which are per-pillar +
  // per-topic over multi-week windows). This one is used by the Personal
  // Question and Heartbeat cron prompts to ground asking / nudge decisions
  // in what actually happened yesterday across Fuel / Journal / Lessons.
  api.registerTool(
    wrap({
      name: "nbhd_yesterdays_signals",
      description:
        "Cross-pillar snapshot of yesterday's activity across Fuel (workouts), Journal (entries, energy), and Lessons (approved, pending). Includes 'today_so_far' to catch late-logging and 'notable_gaps' flags (e.g. 'journal_dark_3_days') the backend pre-computes as cheap hints — you decide whether to act on them. Use this before deciding whether to ask a signal-driven Personal Question or to ground a Heartbeat nudge in a fresh fact. Tenant-tz-aware; 'yesterday' is the previous calendar day in the user's local timezone. The Core pillar is intentionally omitted (no data model yet).",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute(_id, _params) {
        const payload = await callRuntime(api, {
          path: insightsPath(api, "/yesterdays-signals/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
