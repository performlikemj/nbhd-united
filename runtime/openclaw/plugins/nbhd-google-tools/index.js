const DEFAULT_REQUEST_TIMEOUT_MS = 20000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function asStringArray(value, { maxItems = 10 } = {}) {
  if (value === undefined || value === null) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw new Error("Expected an array of strings");
  }
  if (value.length > maxItems) {
    throw new Error(`Array exceeds max items (${maxItems})`);
  }

  const cleaned = [];
  for (const item of value) {
    if (typeof item !== "string") {
      throw new Error("Array must contain only strings");
    }
    const normalized = item.trim();
    if (normalized.length > 0) {
      cleaned.push(normalized);
    }
  }
  return cleaned;
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") {
    return defaultValue;
  }
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) {
    return defaultValue;
  }
  return Math.max(min, Math.min(max, parsed));
}

function parseBoolean(value, defaultValue = false) {
  if (value === undefined || value === null || value === "") {
    return defaultValue;
  }
  const normalized = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return defaultValue;
}

function getRuntimeConfig(api) {
  const pluginConfig = asObject(api.pluginConfig);
  const apiBaseUrl = asTrimmedString(pluginConfig.apiBaseUrl || process.env.NBHD_API_BASE_URL).replace(
    /\/+$/,
    "",
  );
  const tenantId = asTrimmedString(process.env.NBHD_TENANT_ID);
  const internalKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 60000,
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

function buildUrl(baseUrl, path, query) {
  const url = new URL(`${baseUrl}${path}`);
  for (const [key, value] of Object.entries(query || {})) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    url.searchParams.set(key, String(value));
  }
  return url;
}

function renderPayload(payload) {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(payload, null, 2),
      },
    ],
    details: {
      json: payload,
    },
  };
}

async function callNbhdRuntimeRequest(api, { path, method = "GET", query, body }) {
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
      const providerStatus = normalized.provider_status;
      const detailSuffix = detail ? ` (${detail})` : "";
      const providerSuffix =
        providerStatus !== undefined && providerStatus !== null
          ? ` [provider_status=${providerStatus}]`
          : "";
      throw new Error(`NBHD runtime error ${response.status}: ${code}${detailSuffix}${providerSuffix}`);
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

function registerTool(api, tool) {
  api.registerTool(tool, { optional: true });
}

export default function register(api) {
  registerTool(api, {
    name: "nbhd_gmail_list_messages",
    description: "List recent Gmail messages for the tenant (read-only).",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        q: { type: "string", description: "Gmail search query." },
        max_results: { type: "number", minimum: 1, maximum: 10 },
      },
    },
    async execute(_id, params) {
      const input = asObject(params);
      const payload = await callNbhdRuntimeRequest(api, {
        path: tenantPath(api, "/gmail/messages/"),
        method: "GET",
        query: {
          q: asTrimmedString(input.q),
          max_results: parseInteger(input.max_results, {
            defaultValue: 5,
            min: 1,
            max: 10,
          }),
        },
      });
      return renderPayload(payload);
    },
  });

  registerTool(api, {
    name: "nbhd_gmail_get_message_detail",
    description: "Get normalized Gmail message detail (body + thread context) for action-item extraction.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        message_id: { type: "string" },
        include_thread: { type: "boolean" },
        thread_limit: { type: "number", minimum: 1, maximum: 10 },
      },
      required: ["message_id"],
    },
    async execute(_id, params) {
      const input = asObject(params);
      const messageId = asTrimmedString(input.message_id);
      if (!messageId) {
        throw new Error("message_id is required");
      }
      const payload = await callNbhdRuntimeRequest(api, {
        path: tenantPath(api, `/gmail/messages/${encodeURIComponent(messageId)}/`),
        method: "GET",
        query: {
          include_thread: parseBoolean(input.include_thread, true),
          thread_limit: parseInteger(input.thread_limit, {
            defaultValue: 5,
            min: 1,
            max: 10,
          }),
        },
      });
      return renderPayload(payload);
    },
  });

  registerTool(api, {
    name: "nbhd_calendar_list_events",
    description: "List upcoming Google Calendar events for the tenant (read-only).",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        time_min: { type: "string" },
        time_max: { type: "string" },
        max_results: { type: "number", minimum: 1, maximum: 20 },
      },
    },
    async execute(_id, params) {
      const input = asObject(params);
      const payload = await callNbhdRuntimeRequest(api, {
        path: tenantPath(api, "/google-calendar/events/"),
        method: "GET",
        query: {
          time_min: asTrimmedString(input.time_min),
          time_max: asTrimmedString(input.time_max),
          max_results: parseInteger(input.max_results, {
            defaultValue: 10,
            min: 1,
            max: 20,
          }),
        },
      });
      return renderPayload(payload);
    },
  });

  registerTool(api, {
    name: "nbhd_calendar_get_freebusy",
    description: "Get busy windows from the tenant's primary Google Calendar (read-only).",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        time_min: { type: "string" },
        time_max: { type: "string" },
      },
    },
    async execute(_id, params) {
      const input = asObject(params);
      const payload = await callNbhdRuntimeRequest(api, {
        path: tenantPath(api, "/google-calendar/freebusy/"),
        method: "GET",
        query: {
          time_min: asTrimmedString(input.time_min),
          time_max: asTrimmedString(input.time_max),
        },
      });
      return renderPayload(payload);
    },
  });

}
