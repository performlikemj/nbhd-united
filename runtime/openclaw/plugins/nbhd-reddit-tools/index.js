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

function renderText(text) {
  return {
    content: [{ type: "text", text: String(text) }],
  };
}

function renderPayload(payload) {
  return renderText(JSON.stringify(payload, null, 2));
}

async function callIntegrationsApi(api, path, options = {}) {
  const runtime = getRuntimeConfig(api);
  const url = `${runtime.apiBaseUrl}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);
  try {
    const response = await fetch(url, {
      headers: {
        "Content-Type": "application/json",
        "X-NBHD-Internal-Key": runtime.internalKey,
        "X-NBHD-Tenant-Id": runtime.tenantId,
      },
      signal: controller.signal,
      ...options,
    });
    const raw = await response.text();
    let payload = {};
    try { payload = JSON.parse(raw); } catch { payload = { raw }; }
    return { ok: response.ok, status: response.status, data: payload };
  } catch (error) {
    if (error && error.name === "AbortError") throw new Error(`Request timed out`);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function callRedditTool(api, { action, params = {} }) {
  const runtime = getRuntimeConfig(api);
  const url = `${runtime.apiBaseUrl}/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}/reddit/tool/`;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const headers = {
      "Content-Type": "application/json",
      "X-NBHD-Internal-Key": runtime.internalKey,
      "X-NBHD-Tenant-Id": runtime.tenantId,
    };

    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ action, ...params }),
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

function registerTool(api, tool) {
  api.registerTool(tool, { optional: true });
}

export default function register(api) {
  registerTool(api, {
    name: "nbhd_reddit_connect",
    description: "Connect the user's Reddit account via OAuth. Call when user asks to connect Reddit.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {},
    },
    async execute(_id, _params) {
      const payload = await callRedditTool(api, { action: "connect" });
      return renderPayload(payload);
    },
  });

  registerTool(api, {
    name: "nbhd_reddit_status",
    description: "Check if the user's Reddit account is connected.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {},
    },
    async execute(_id, _params) {
      const runtime = getRuntimeConfig(api);
      const { ok, data } = await callIntegrationsApi(api,
        `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}/reddit/status/`
      );
      if (!ok) {
        return renderText("Reddit is not connected. Use nbhd_reddit_connect to link your account.");
      }
      const connected = data.connected === true;
      const username = asTrimmedString(data.username || data.provider_email || "");
      if (connected) {
        return renderText(`Reddit is connected${username ? ` as ${username}` : ""}.`);
      }
      return renderText("Reddit is not connected. Use nbhd_reddit_connect to link your account.");
    },
  });

  registerTool(api, {
    name: "nbhd_reddit_digest",
    description:
      "Get top posts from one or more subreddits. If the user's request is ambiguous or no subreddit is specified, ask which subreddit(s) they want before calling this tool.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        subreddits: {
          type: "array",
          items: { type: "string" },
          maxItems: 10,
          description: "List of subreddit names to fetch (without r/ prefix). Required — ask the user if not specified.",
        },
        sort: {
          type: "string",
          enum: ["hot", "new", "top", "rising"],
          description: "Sort order. Defaults to hot.",
        },
        limit: {
          type: "number",
          minimum: 1,
          maximum: 20,
          description: "Number of posts to return per subreddit (1-20). Defaults to 5.",
        },
      },
      required: ["subreddits"],
    },
    async execute(_id, params) {
      const input = asObject(params);
      const subreddits = asStringArray(input.subreddits, { maxItems: 10 });
      const sortRaw = asTrimmedString(input.sort);
      const validSorts = ["hot", "new", "top", "rising"];
      const sort = validSorts.includes(sortRaw) ? sortRaw : "hot";
      const limit = parseInteger(input.limit, { defaultValue: 5, min: 1, max: 20 });

      if (subreddits.length === 0) {
        return "Which subreddit(s) would you like me to check? (e.g. r/soccer, r/machinelearning)";
      }

      // Fetch each subreddit separately — REDDIT_GET_R_TOP takes one subreddit at a time
      const parts = [];
      for (const subreddit of subreddits) {
        const payload = await callRedditTool(api, {
          action: "digest",
          params: { subreddit, sort, limit },
        });
        parts.push(`**r/${subreddit}**\n${JSON.stringify(payload, null, 2)}`);
      }
      return renderText(parts.join("\n\n"));
    },
  });

  registerTool(api, {
    name: "nbhd_reddit_my_activity",
    description: "Check for replies to the user's recent Reddit posts and comments.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {},
    },
    async execute(_id, _params) {
      const payload = await callRedditTool(api, { action: "my_activity" });
      return renderPayload(payload);
    },
  });

  registerTool(api, {
    name: "nbhd_reddit_post",
    description:
      "Submit a new Reddit post. IMPORTANT: Always show the draft to the user and get explicit approval before calling this tool.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        subreddit: {
          type: "string",
          description: "Subreddit name to post to (without r/ prefix).",
        },
        title: {
          type: "string",
          maxLength: 300,
          description: "Post title (max 300 characters).",
        },
        text: {
          type: "string",
          description: "Post body text (required for self posts).",
        },
        kind: {
          type: "string",
          enum: ["self", "link"],
          description: "Post kind. Defaults to self.",
        },
      },
      required: ["subreddit", "title"],
    },
    async execute(_id, params) {
      const input = asObject(params);

      const subreddit = asTrimmedString(input.subreddit);
      if (!subreddit) {
        throw new Error("subreddit is required");
      }

      const title = asTrimmedString(input.title);
      if (!title) {
        throw new Error("title is required");
      }
      if (title.length > 300) {
        throw new Error("title must not exceed 300 characters");
      }

      const kindRaw = asTrimmedString(input.kind);
      const kind = kindRaw === "link" ? "link" : "self";

      const text = asTrimmedString(input.text);
      if (kind === "self" && !text) {
        throw new Error("text is required for self posts");
      }

      const payload = await callRedditTool(api, {
        action: "post",
        params: { subreddit, title, text: text || undefined, kind },
      });
      return renderPayload(payload);
    },
  });

  registerTool(api, {
    name: "nbhd_reddit_reply",
    description:
      "Reply to a Reddit post or comment. IMPORTANT: Always show the draft to the user and get explicit approval before calling this tool.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        thing_id: {
          type: "string",
          description: "Reddit fullname ID of the post or comment to reply to (e.g. t3_abc123 or t1_abc123).",
        },
        text: {
          type: "string",
          description: "Reply text body.",
        },
      },
      required: ["thing_id", "text"],
    },
    async execute(_id, params) {
      const input = asObject(params);

      const thingId = asTrimmedString(input.thing_id);
      if (!thingId) {
        throw new Error("thing_id is required");
      }
      if (!/^t[1-9]_[a-z0-9]+$/i.test(thingId)) {
        throw new Error("thing_id must be a valid Reddit fullname (e.g. t3_abc123 or t1_abc123)");
      }

      const text = asTrimmedString(input.text);
      if (!text) {
        throw new Error("text is required");
      }

      const payload = await callRedditTool(api, {
        action: "reply",
        params: { thing_id: thingId, text },
      });
      return renderPayload(payload);
    },
  });
}
