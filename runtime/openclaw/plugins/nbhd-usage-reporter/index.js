/**
 * NBHD Usage Reporter Plugin
 *
 * Reports LLM usage from agent turns back to the NBHD control-plane so
 * token/message counters stay synchronized in polling mode.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 5000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function asNonNegativeInteger(value) {
  if (value === undefined || value === null || value === "") {
    return null;
  }

  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed) || parsed < 0) {
    return null;
  }

  return parsed;
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

  const apiBaseUrl = asTrimmedString(
    pluginConfig.apiBaseUrl || envBase,
  ).replace(/\/+$/, "");
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

function extractUsage(event = {}) {
  const usage = asObject(event.usage);
  const inputTokens = asNonNegativeInteger(
    usage.input_tokens ?? usage.input,
  );
  const outputTokens = asNonNegativeInteger(
    usage.output_tokens ?? usage.output,
  );
  const modelUsed = asTrimmedString(event.model || event.model_used || "");

  if (inputTokens === null || outputTokens === null || !modelUsed) {
    return null;
  }

  return {
    event_type: "message",
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    model_used: modelUsed,
  };
}

async function reportUsage(payload, api) {
  let runtime;
  try {
    runtime = getRuntimeConfig(api);
  } catch (error) {
    api.logger.debug(`Skipping usage report registration: ${error.message}`);
    return;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const url = new URL(
      `/api/v1/internal/runtime/${encodeURIComponent(runtime.tenantId)}/usage/report/`,
      runtime.apiBaseUrl,
    );

    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-NBHD-Internal-Key": runtime.internalKey,
        "X-NBHD-Tenant-Id": runtime.tenantId,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`NBHD usage report failed (${response.status}): ${text}`);
    }
  } catch (error) {
    if (error && error.name === "AbortError") {
      api.logger.error("NBHD usage report timed out");
      return;
    }
    api.logger.error(`NBHD usage report failed: ${error.message}`);
  } finally {
    clearTimeout(timeout);
  }
}

export default function register(api) {
  if (!api || typeof api.on !== "function") {
    return;
  }

  api.on("llm_output", (event) => {
    const payload = extractUsage(event);
    if (!payload) {
      return;
    }

    void reportUsage(payload, api);
  });
}
