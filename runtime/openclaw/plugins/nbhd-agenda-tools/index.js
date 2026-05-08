/**
 * NBHD Agenda Tools Plugin (Phase D)
 *
 * Exposes agent-facing tools for the agenda-aware assistant arc:
 *
 * - nbhd_record_commitment: write a future-aware "note to self" that
 *   the platform will surface back at a well-chosen moment, not an
 *   exact-time alarm. The renderer adds it to the Agenda envelope
 *   section once `surface_after` has passed; the agent decides per
 *   moment whether to weave the commitment into the current turn.
 *
 * Design note — why a tool instead of a Bash + curl callback:
 * earlier today we shipped the welcome flow with the agent doing a
 * Bash + curl to mark delivery. That path only works for Claude
 * (the only model with Bash). When we switched to OpenRouter
 * (kimi-k2.6), the welcome message went out but the callback didn't
 * run — kimi tried `nbhd_platform_issue_report` instead because Bash
 * wasn't available. The lesson: model-agnostic tools belong in
 * plugins, not in shell-out instructions.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 5000;

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") return defaultValue;
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) return defaultValue;
  return Math.max(min, Math.min(max, parsed));
}

function getRuntimeConfig(api) {
  const envBase = asTrimmedString(process.env.NBHD_API_BASE_URL);
  const envTenant = asTrimmedString(process.env.NBHD_TENANT_ID);
  const envKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const pluginConfig = asObject(api && api.pluginConfig);

  const apiBaseUrl = asTrimmedString(pluginConfig.apiBaseUrl || envBase).replace(/\/+$/, "");
  const tenantId = asTrimmedString(pluginConfig.tenantId || envTenant);
  const internalKey = asTrimmedString(pluginConfig.internalApiKey || envKey);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 30000,
  });

  if (!apiBaseUrl) {
    throw new Error("NBHD_API_BASE_URL is required");
  }
  if (!tenantId) {
    throw new Error("NBHD_TENANT_ID is required");
  }
  if (!internalKey) {
    throw new Error("NBHD_INTERNAL_API_KEY is required");
  }
  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

async function postJson(url, body, headers, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await resp.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = { raw: text };
    }
    if (!resp.ok) {
      const detail = parsed && (parsed.error || parsed.detail) ? `${parsed.error || parsed.detail}` : `HTTP ${resp.status}`;
      throw new Error(`NBHD runtime ${resp.status}: ${detail}`);
    }
    return parsed;
  } finally {
    clearTimeout(timeout);
  }
}

export default function register(api) {
  if (!api || typeof api.registerTool !== "function") {
    return;
  }

  api.logger.info("NBHD agenda tools plugin registered");

  api.registerTool(
    {
      name: "nbhd_record_commitment",
      description:
        "Record a future-aware commitment to follow up with the user about a topic. " +
        "The platform surfaces the commitment back to you at a well-chosen moment after " +
        "`surface_after` — never as a hard alarm. Use this when you want to remember to " +
        "raise a topic later, but only when the moment fits naturally.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          about: {
            type: "string",
            description:
              "Brief topic (1 sentence). E.g. 'check in on debt payoff progress', " +
              "'ask how the new running plan is going'.",
          },
          surface_after: {
            type: "string",
            description:
              "ISO-8601 timestamp. Earliest time at which the platform will treat the " +
              "commitment as eligible. The renderer + agent then decide based on context " +
              "fit. Use future timestamps; commitments past their date stay eligible " +
              "until acted on or abandoned.",
          },
          why: {
            type: "string",
            description:
              "Why you're committing — context for future-you when surfacing. E.g. " +
              "'user said they wanted to revisit in 2 weeks', 'they were stressed about " +
              "this and wanted breathing room'.",
          },
        },
        required: ["about", "surface_after", "why"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const about = asTrimmedString(input.about);
        const why = asTrimmedString(input.why);
        const surfaceAfter = asTrimmedString(input.surface_after);

        if (!about) throw new Error("about is required");
        if (!surfaceAfter) throw new Error("surface_after is required (ISO-8601)");
        if (!why) throw new Error("why is required");

        const { apiBaseUrl, tenantId, internalKey, requestTimeoutMs } = getRuntimeConfig(api);
        const url = `${apiBaseUrl}/api/v1/tenants/runtime/${encodeURIComponent(tenantId)}/commitments/`;
        const result = await postJson(
          url,
          { about, surface_after: surfaceAfter, why },
          {
            "X-NBHD-Internal-Key": internalKey,
            "X-NBHD-Tenant-Id": tenantId,
          },
          requestTimeoutMs,
        );
        return {
          recorded: true,
          item_id: result?.item_id,
          surface_after: result?.surface_after,
        };
      },
    },
    { optional: true },
  );
}
