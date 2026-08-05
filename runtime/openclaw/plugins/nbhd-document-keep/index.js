import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-document-keep" });

/**
 * NBHD Document Information-Keeping Tools
 *
 * The uploaded file is ephemeral (GC'd ~24h after arrival). What persists is the
 * *information* the user agreed to keep, routed to its real destination through the
 * normal typed write tools. These three tools record and remove that provenance:
 *
 *  - nbhd_document_keep: after the user agrees and you've written each item with the
 *    normal typed tools, file ONE call recording that those items came from this
 *    document — so they can be forgotten later as a unit. The server VALIDATES every
 *    id against a real tenant-owned row before recording; it can't record a
 *    hallucinated/stale reference.
 *  - nbhd_document_list_ingestions: find WHICH document the user means (filename,
 *    when, whether the file already expired, and the saved items) before forgetting.
 *  - nbhd_document_forget: remove every item that came from one document — and
 *    nothing else — reporting per-item what was removed and what couldn't be.
 *
 * config_generator loads this plugin only for tenants with document_ingestion_enabled
 * and injects `documentIngestionEnabled: true`; we register the tools only when it is
 * strictly true (fail-closed, mirroring nbhd-friends-tools' proposeEnabled).
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

// Normalize the agent-supplied artifacts[] into the runtime's expected shape,
// tolerating loose types (drop anything without a type + id).
function normalizeArtifacts(rawArtifacts) {
  if (!Array.isArray(rawArtifacts)) return [];
  const out = [];
  for (const item of rawArtifacts) {
    const art = asObject(item);
    const objectType = asTrimmedString(art.object_type);
    const objectId = asTrimmedString(art.object_id) || String(art.object_id ?? "").trim();
    if (!objectType || !objectId) continue;
    out.push({
      kind: asTrimmedString(art.kind),
      object_type: objectType,
      object_id: objectId,
      destination: asTrimmedString(art.destination),
      excerpt: asTrimmedString(art.excerpt),
    });
  }
  return out;
}

export default function register(api) {
  const cfg = api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
  // Fail-closed: only register when the flag is strictly true. config_generator
  // injects it true whenever it loads this plugin; anything else → no tools.
  if (cfg.documentIngestionEnabled !== true) return;

  // ── Record provenance for what was saved from a document ─────────────────
  api.registerTool(
    wrap({
      name: "nbhd_document_keep",
      description:
        "AFTER the user agrees and you've saved each item with the normal typed tools, file ONE call recording that those items came from this source, so they can be removed later as a unit. Works for an uploaded document AND for information you read from a Gmail message, a calendar event, or a Reddit post — for those, set `source_kind` and `source_ref` (the source id) in place of a filename. Pass each saved item with its destination and the object id the write tool returned. Do this in the SAME turn you saved them, before you tell the user it's done. The server validates every id against a real saved row — if it reports it couldn't confirm an item (in `errors`), tell the user that item may not have saved cleanly and re-check it; don't claim it's kept.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          original_filename: {
            type: "string",
            description:
              "Human label for this source: an uploaded document's filename (e.g. 'invoice-oct.pdf'), or the email subject / event title / post title for a non-upload source.",
          },
          source_kind: {
            type: "string",
            description:
              "Where the kept info came from: 'upload' (default), 'email', 'calendar', or 'reddit'. Set this for anything you read rather than a file the user uploaded.",
          },
          source_ref: {
            type: "string",
            description:
              "Required for a non-upload source_kind — the source's namespaced id so 'forget everything from that email' can group these items: 'gmail:<message-id>', 'gcal:<event-id>', or 'reddit:<t3_/t1_-fullname>'.",
          },
          workspace_path: {
            type: "string",
            description: "The attachment path from the [Document attached: <path>] marker (uploads only).",
          },
          content_hash: {
            type: "string",
            description: "Optional short content hash from the stored filename (doc_<hash>.pdf → <hash>).",
          },
          client_msg_id: {
            type: "string",
            description: "Optional id of the upload turn, if known — enables a completeness check.",
          },
          artifacts: {
            type: "array",
            description: "One entry per saved item.",
            items: {
              type: "object",
              additionalProperties: false,
              properties: {
                kind: {
                  type: "string",
                  description: "journal_note | task | goal | reminder | verbatim_note",
                },
                object_type: {
                  type: "string",
                  description: "Django label of the saved row: journal.Document, journal.Task, journal.Goal, or cron.CronJob.",
                },
                object_id: {
                  type: "string",
                  description: "The id the write tool returned (a document/task/goal uuid, or a reminder's name).",
                },
                destination: {
                  type: "string",
                  description: "Short human label for the console + your list (e.g. 'Reminder for Oct 31').",
                },
                excerpt: {
                  type: "string",
                  description: "The actual text/values you saved — shown back to the user; survives deletion.",
                },
              },
              required: ["object_type", "object_id"],
            },
          },
        },
        required: ["original_filename", "artifacts"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/documents/keep/"),
          method: "POST",
          body: {
            source: {
              original_filename: asTrimmedString(input.original_filename),
              source_kind: asTrimmedString(input.source_kind),
              source_ref: asTrimmedString(input.source_ref),
              workspace_path: asTrimmedString(input.workspace_path),
              content_hash: asTrimmedString(input.content_hash),
              client_msg_id: asTrimmedString(input.client_msg_id),
            },
            artifacts: normalizeArtifacts(input.artifacts),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── List recent document ingestions (to confirm WHICH one to forget) ─────
  api.registerTool(
    wrap({
      name: "nbhd_document_list_ingestions",
      description:
        "List the documents the user has saved information from — filename, when, whether the file itself has already expired, status, and the saved items with their destinations. Use this to confirm WHICH document the user means before forgetting, showing them what was saved from it.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          limit: {
            type: "integer",
            description: "How many recent ingestions to return (default 20, max 100).",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, "/documents/ingestions/"),
          method: "GET",
          query: { limit: input.limit },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Forget everything from one document ──────────────────────────────────
  api.registerTool(
    wrap({
      name: "nbhd_document_forget",
      description:
        "Remove every item that was saved from ONE document — and nothing else. Confirm which document first with nbhd_document_list_ingestions. Report exactly what was removed and what couldn't be: the response's `results` lists each item, and `caveats` lists what forget cannot do (a reminder that already fired stays in history; the document's contents already reached the AI model and can't be un-read; to forget a person's name use People settings). Never guess which document, and never delete by hand.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          ingestion_id: {
            type: "string",
            description: "The id of the ingestion to forget (from nbhd_document_list_ingestions).",
          },
        },
        required: ["ingestion_id"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: tenantPath(api, `/documents/${encodeURIComponent(asTrimmedString(input.ingestion_id))}/forget/`),
          method: "POST",
          body: {},
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
