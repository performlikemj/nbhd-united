/**
 * NBHD Cron Enforcement Plugin
 *
 * Fire-time enforcement layer for typed cron patterns. Three responsibilities:
 *
 *   1. cron_changed(action="started")
 *        Fetch this cron's pattern context (pattern name, typed_payload,
 *        pre-rendered prompt injection) from Django. Cache by sessionKey
 *        for the duration of the run. Without this cache, the other hooks
 *        can't tell a typed-cron run apart from a chat reply.
 *
 *   2. before_prompt_build
 *        If the current sessionKey is a cached typed-cron run, return the
 *        pattern's prompt injection via `appendSystemContext`. We pick
 *        appendSystemContext (not prependSystemContext) on purpose —
 *        nbhd-routing-context already uses prependSystemContext, and hook
 *        return values from sibling plugins may collide. Different fields,
 *        no collision.
 *
 *   3. message_sending
 *        For typed-cron runs, POST the outbound content to Django's
 *        validate_outbound endpoint. If validation fails, rewrite the
 *        content with the pattern's fallback message rather than ship a
 *        broken claim.
 *
 *   4. cron_changed(action="finished" | "removed")
 *        Drop the cache entry.
 *
 * Hook contract verified against `dist/plugin-sdk/src/plugins/hook-types.d.ts`
 * in openclaw@2026.5.7.
 *
 * The toolsAllow restriction baked into each pattern's OC payload at
 * create time is the structural mutation guard (cron-turn agents
 * literally cannot call nbhd_task_create etc.). This plugin handles
 * the soft validation layer on top.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 8000;
const DEFAULT_CACHE_TTL_SECONDS = 600;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
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
  const pluginConfig = asObject(api && api.pluginConfig);
  const apiBaseUrl = asTrimmedString(
    pluginConfig.apiBaseUrl || process.env.NBHD_API_BASE_URL,
  ).replace(/\/+$/, "");
  const tenantId = asTrimmedString(process.env.NBHD_TENANT_ID);
  const internalKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 30000,
  });
  const cacheTtlMs =
    parseInteger(pluginConfig.cacheTtlSeconds, {
      defaultValue: DEFAULT_CACHE_TTL_SECONDS,
      min: 60,
      max: 1800,
    }) * 1000;
  if (!apiBaseUrl || !tenantId || !internalKey) return null;
  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs, cacheTtlMs };
}

async function djangoRequest(runtime, { path, method = "GET", body }) {
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
    if (!response.ok) {
      return { ok: false, status: response.status };
    }
    const raw = await response.text();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = { raw };
      }
    }
    return { ok: true, payload: asObject(payload) };
  } catch (error) {
    return { ok: false, error };
  } finally {
    clearTimeout(timeout);
  }
}

function tenantPath(runtime, suffix) {
  return `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

// In-process cache of pattern-context per sessionKey. Cleared on
// cron_changed(finished) or after TTL expiry. The cache is per-process,
// per-tenant container — fine because each tenant has its own OC
// container instance.
//
// Shape: Map<sessionKey, { fetchedAtMs, pattern, typed_payload, name,
//                          prompt_injection, cronName, ttlMs }>
const runContextCache = new Map();

function cacheKey(sessionKey, runId) {
  // Prefer sessionKey when present; fall back to runId. Both come from
  // PluginHookAgentContext.
  return asTrimmedString(sessionKey) || asTrimmedString(runId) || "";
}

function pruneExpired() {
  const now = Date.now();
  for (const [key, entry] of runContextCache.entries()) {
    if (now - entry.fetchedAtMs > entry.ttlMs) {
      runContextCache.delete(key);
    }
  }
}

async function fetchPatternContext(runtime, cronName, logger) {
  const result = await djangoRequest(runtime, {
    path: tenantPath(
      runtime,
      `/crons/${encodeURIComponent(cronName)}/pattern_context/`,
    ),
    method: "GET",
  });
  if (!result.ok) {
    if (result.status === 404) {
      // Not a typed cron — that's expected for legacy/freeform runs.
      return null;
    }
    if (logger && typeof logger.warn === "function") {
      logger.warn(
        `nbhd-cron-enforcement: pattern_context fetch failed cron=${cronName} ` +
        `status=${result.status || "?"} error=${(result.error && result.error.message) || ""}`,
      );
    }
    return null;
  }
  return result.payload;
}

async function validateOutbound(runtime, cronName, content) {
  const result = await djangoRequest(runtime, {
    path: tenantPath(
      runtime,
      `/crons/${encodeURIComponent(cronName)}/validate_outbound/`,
    ),
    method: "POST",
    body: { content },
  });
  if (!result.ok) {
    // Open on transport failure — don't strip user content because Django
    // is unreachable. Logged via caller.
    return { ok: true, transport_error: true };
  }
  return result.payload || { ok: true };
}

export default function register(api) {
  if (!api || typeof api.on !== "function") return;

  const runtime = getRuntimeConfig(api);
  if (!runtime) {
    // Required env vars missing — plugin loads (so the manifest validates)
    // but hooks short-circuit. Production paths always have these set.
    if (api.logger && typeof api.logger.warn === "function") {
      api.logger.warn(
        "nbhd-cron-enforcement: NBHD_API_BASE_URL / NBHD_TENANT_ID / " +
        "NBHD_INTERNAL_API_KEY not set — enforcement disabled.",
      );
    }
    return;
  }

  api.logger.info(
    "nbhd-cron-enforcement: registered (cron_changed + before_prompt_build + message_sending)",
  );

  // ── cron_changed ──────────────────────────────────────────────────────
  api.on("cron_changed", async (event) => {
    pruneExpired();
    const action = asTrimmedString(event && event.action);
    if (action !== "started" && action !== "finished" && action !== "removed") {
      return;
    }
    const sessionKey = cacheKey(event && event.sessionKey, event && event.runId);
    if (!sessionKey) return;

    if (action === "finished" || action === "removed") {
      runContextCache.delete(sessionKey);
      return;
    }

    // action === "started"
    const cronName = asTrimmedString(
      (event && event.job && event.job.name) || event.jobId,
    );
    if (!cronName) return;

    try {
      const ctx = await fetchPatternContext(runtime, cronName, api.logger);
      if (ctx) {
        runContextCache.set(sessionKey, {
          fetchedAtMs: Date.now(),
          pattern: asTrimmedString(ctx.pattern),
          typed_payload: asObject(ctx.typed_payload),
          name: asTrimmedString(ctx.name) || cronName,
          prompt_injection: asTrimmedString(ctx.prompt_injection),
          cronName,
          ttlMs: runtime.cacheTtlMs,
        });
      }
    } catch (error) {
      api.logger.warn(
        `nbhd-cron-enforcement: cron_changed fetch error cron=${cronName} ` +
        `error=${(error && error.message) || "?"}`,
      );
    }
  });

  // ── before_prompt_build ───────────────────────────────────────────────
  api.on("before_prompt_build", (_event, ctx) => {
    const sessionKey = cacheKey(ctx && ctx.sessionKey, ctx && ctx.runId);
    if (!sessionKey) return undefined;
    const entry = runContextCache.get(sessionKey);
    if (!entry || !entry.prompt_injection) return undefined;
    // appendSystemContext keeps us out of the way of nbhd-routing-context
    // which uses prependSystemContext.
    return { appendSystemContext: `\n\n${entry.prompt_injection}\n` };
  });

  // ── message_sending ───────────────────────────────────────────────────
  api.on("message_sending", async (event, ctx) => {
    const sessionKey = cacheKey(ctx && ctx.sessionKey, ctx && ctx.runId);
    if (!sessionKey) return undefined;
    const entry = runContextCache.get(sessionKey);
    if (!entry) return undefined; // not a typed-cron run

    const content = asTrimmedString(event && event.content);
    if (!content) return undefined;

    try {
      const result = await validateOutbound(runtime, entry.cronName, content);
      if (result && result.ok === false) {
        const fallback = asTrimmedString(result.fallback_content);
        api.logger.warn(
          `nbhd-cron-enforcement: outbound validation failed cron=${entry.cronName} ` +
          `pattern=${entry.pattern} reason=${asTrimmedString(result.reason) || "?"} ` +
          `len=${content.length} — substituting fallback`,
        );
        if (fallback) return { content: fallback };
        return { cancel: true, cancelReason: "typed_cron_validation_failed" };
      }
    } catch (error) {
      api.logger.warn(
        `nbhd-cron-enforcement: validate_outbound error cron=${entry.cronName} ` +
        `error=${(error && error.message) || "?"} — shipping unvalidated`,
      );
    }
    return undefined;
  });
}
