/**
 * NBHD Stream Progress Plugin
 *
 * The pseudo-streaming half of the agent activity stream (sibling of
 * nbhd-activity-stream). OpenClaw 2026.5.28 has no token/delta hook, so the
 * finest text-bearing signal is per model-call step: `llm_output` carries that
 * step's `assistantTexts`, and `before_agent_finalize` carries the resolved
 * `lastAssistantMessage`. As the agent works a turn this accumulates the text so
 * far and POSTs it — `{text, seq}` with a monotonically increasing seq — to the
 * SAME control-plane progress endpoint the phase narrator uses
 * (`/api/v1/internal/runtime/<tenant>/chat/progress/`). A polling client renders
 * that partial text instead of waiting for the whole reply.
 *
 * The hook event carries only the OpenClaw run (not the inbound client_msg_id),
 * so the POST omits client_msg_id and the control plane attributes the partial
 * text to the app/Siri turn that currently holds a live in-flight drain lease
 * (turns are serialized per container). A Telegram/LINE turn holds no such
 * app-channel lease, so its partial POST attributes to no row — a harmless
 * no-op, never bleeding another channel's reply into an app turn's stream. The
 * control plane seq-guards the write so an out-of-order/duplicate POST can't
 * rewind the stream.
 *
 * Fail-open + non-blocking: `llm_output` and `before_agent_finalize` never block
 * the turn, so the POST is fire-and-forget (never awaited, never throws) and the
 * hooks always return undefined. Opt-in: dormant unless enabled
 * (OPENCLAW_STREAM_PROGRESS_PLUGIN_ID) so it adds no fleet load until the client
 * consumes `partial_text`. Hook contract verified against openclaw 2026.5.28
 * hook-types (PluginHookLlmOutputEvent.assistantTexts,
 * PluginHookBeforeAgentFinalizeEvent.lastAssistantMessage).
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 4000;
// Mirror the control plane's partial_text truncation so we never ship a payload
// the server will only clip anyway.
const MAX_PARTIAL_CHARS = 32000;

function asString(value) {
  return typeof value === "string" ? value : "";
}

// Pure (exported for tests): join one step's assistantTexts into a single string,
// dropping non-string/empty pieces.
export function joinAssistantTexts(assistantTexts) {
  if (!Array.isArray(assistantTexts)) return "";
  return assistantTexts.filter((t) => typeof t === "string" && t.length > 0).join("");
}

// Pure (exported for tests): given the prior cumulative text and a new step's
// assistantTexts, return the new cumulative text (appended, capped). An empty
// step leaves the accumulator unchanged.
export function accumulate(prev, assistantTexts) {
  const base = asString(prev);
  const chunk = joinAssistantTexts(assistantTexts);
  if (!chunk) return base;
  const combined = base + chunk;
  return combined.length > MAX_PARTIAL_CHARS ? combined.slice(0, MAX_PARTIAL_CHARS) : combined;
}

// Pure (exported for tests): clamp any final text to the same cap.
export function capText(text) {
  const s = asString(text);
  return s.length > MAX_PARTIAL_CHARS ? s.slice(0, MAX_PARTIAL_CHARS) : s;
}

// Monotonic sequence generator (exported for tests). Module-global so seq keeps
// climbing across turns; the control plane compares against each PENDING row's
// own partial_seq (which starts at 0), so a globally-increasing seq always
// applies to a fresh turn and never rewinds within one.
let _seqCounter = 0;
export function nextSeq() {
  _seqCounter += 1;
  return _seqCounter;
}

function getRuntimeConfig() {
  const apiBaseUrl = asString(process.env.NBHD_API_BASE_URL).trim().replace(/\/+$/, "");
  const tenantId = asString(process.env.NBHD_TENANT_ID).trim();
  const internalKey = asString(process.env.NBHD_INTERNAL_API_KEY).trim();
  if (!apiBaseUrl || !tenantId || !internalKey) return null;
  return { apiBaseUrl, tenantId, internalKey };
}

// Fire-and-forget POST of the partial text. Returns a promise so callers can
// `void` it; never rejects.
export async function postPartial(text, seq, api) {
  const cfg = getRuntimeConfig();
  if (!cfg) return;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_REQUEST_TIMEOUT_MS);
  try {
    const url = new URL(
      `/api/v1/internal/runtime/${encodeURIComponent(cfg.tenantId)}/chat/progress/`,
      cfg.apiBaseUrl,
    );
    await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-NBHD-Internal-Key": cfg.internalKey,
        "X-NBHD-Tenant-Id": cfg.tenantId,
      },
      body: JSON.stringify({ text, seq }),
      signal: controller.signal,
    });
  } catch (err) {
    // Best-effort streaming — a failed/slow partial ping must never affect the turn.
    if (api && api.logger) {
      try {
        api.logger.debug(`nbhd-stream-progress: partial post failed: ${err && err.message}`);
      } catch (_ignored) {
        // logging must never escalate
      }
    }
  } finally {
    clearTimeout(timer);
  }
}

export default function register(api) {
  if (!api || typeof api.on !== "function") {
    return;
  }
  api.logger.info("NBHD stream-progress plugin registered");

  // Per-run cumulative accumulator. Turns are serialized per container, so a
  // single current-run buffer suffices; a run-id change resets it.
  let runId = null;
  let cumulative = "";

  // Each completed model-call step → append its text and POST the cumulative
  // text-so-far. Never throw; always return undefined.
  api.on("llm_output", (event) => {
    try {
      const evRunId = event && typeof event.runId === "string" ? event.runId : null;
      if (evRunId !== runId) {
        runId = evRunId;
        cumulative = "";
      }
      cumulative = accumulate(cumulative, event && event.assistantTexts);
      if (cumulative) void postPartial(cumulative, nextSeq(), api);
    } catch (_ignored) {
      // never let streaming break a turn
    }
    return undefined;
  });

  // The agent is wrapping up → POST the resolved final message as the last
  // pre-reply partial (falls back to the accumulator if the runtime didn't set
  // lastAssistantMessage). Return undefined so we never interfere with the
  // output-guard plugin's finalize decision.
  api.on("before_agent_finalize", (event) => {
    try {
      const finalText = event && typeof event.lastAssistantMessage === "string" ? event.lastAssistantMessage : "";
      const text = capText(finalText || cumulative);
      if (text) void postPartial(text, nextSeq(), api);
      // Reset for the next turn.
      runId = null;
      cumulative = "";
    } catch (_ignored) {
      // no-op
    }
    return undefined;
  });
}
