/**
 * NBHD Activity Stream Plugin
 *
 * The producer half of the agent activity stream (see nbhd-ios/HER_SIRI_ARCHITECTURE.md
 * §4.3). As the agent works a turn, this narrates what it's doing — "checking your
 * journal", "checking your finances", "composing" — so a polling client can show real
 * activity instead of an opaque spinner (and the iOS-27 Siri Live Activity can map it
 * to progress.localizedDescription).
 *
 * Mechanism: `before_tool_call` → POST {phase:"tool", detail} ; `before_agent_finalize`
 * → POST {phase:"composing"} to the control-plane progress endpoint
 * (`/api/v1/internal/runtime/<tenant>/chat/progress/`). The hook event carries only
 * the OpenClaw run (not the inbound client_msg_id), so the POST omits client_msg_id and
 * the control plane narrates the tenant's in-flight turn (turns are serialized per
 * container; only app/Siri turns create the row it updates — so a Telegram/LINE turn is
 * a harmless no-op there).
 *
 * Fail-soft + non-blocking: `before_tool_call` is FAIL-CLOSED (a throw blocks the tool),
 * so the POST is fire-and-forget (never awaited, never throws) and the hook always
 * returns undefined. Opt-in: dormant unless the plugin is enabled
 * (OPENCLAW_ACTIVITY_STREAM_PLUGIN_ID) so it adds no fleet load until the client consumes
 * `phase`. Hook contract matches nbhd-routing-context (api.on before_tool_call /
 * before_agent_finalize, verified against openclaw 2026.5.7 hook-types).
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 4000;

// The toolSearch meta-tool the model invokes to run a catalog tool by id (mode:"tools",
// the fleet default): a real tool call arrives as tool_call({id:"nbhd_journal_add"}).
const TOOL_DISPATCH_META = "tool_call";

// tool-id substring → friendly spoken phrase. First match wins; order matters.
const TOOL_PHRASES = [
  [/journal|note|daily|reflect/, "checking your journal"],
  [/finance|money|account|payment|transaction|budget|expense/, "checking your finances"],
  [/fuel|workout|exercise|weight|body|fitness/, "looking at your fitness"],
  [/task|goal|agenda|todo|reminder/, "checking your tasks and goals"],
  [/cron|schedule/, "checking your schedule"],
  [/calendar|event/, "checking your calendar"],
  [/contact|people|person/, "checking your contacts"],
  [/lesson|insight/, "reviewing your insights"],
  [/search|web|reddit|browse/, "searching"],
];

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

// The real tool id being dispatched: tool_call({id}) → its id; otherwise toolName.
export function realToolId(event) {
  const name = asTrimmedString(event && event.toolName);
  if (name === TOOL_DISPATCH_META) {
    const p = (event && event.params) || {};
    const raw = p.id ?? p.toolId ?? p.tool ?? p.name ?? "";
    return typeof raw === "string" ? raw.trim() : "";
  }
  return name;
}

// Pure mapping (exported for tests): event → {phase, detail} or null (don't emit).
export function phaseForEvent(event) {
  const id = realToolId(event).toLowerCase();
  if (!id) return null;
  // The catalog meta-tools are the model "looking for a tool" — report as thinking.
  if (id === "tool_search" || id === "tool_describe") {
    return { phase: "thinking", detail: "" };
  }
  for (const [re, phrase] of TOOL_PHRASES) {
    if (re.test(id)) return { phase: "tool", detail: phrase };
  }
  return { phase: "tool", detail: "working on it" };
}

function getRuntimeConfig() {
  const apiBaseUrl = asTrimmedString(process.env.NBHD_API_BASE_URL).replace(/\/+$/, "");
  const tenantId = asTrimmedString(process.env.NBHD_TENANT_ID);
  const internalKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  if (!apiBaseUrl || !tenantId || !internalKey) return null;
  return { apiBaseUrl, tenantId, internalKey };
}

// Fire-and-forget POST. Returns a promise so callers can `void` it; never rejects.
export async function postProgress(phase, detail, api) {
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
      body: JSON.stringify({ phase, detail: detail || "" }),
      signal: controller.signal,
    });
  } catch (err) {
    // Best-effort narration — a failed/slow progress ping must never affect the turn.
    if (api && api.logger) {
      try {
        api.logger.debug(`nbhd-activity-stream: progress post failed: ${err && err.message}`);
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
  api.logger.info("NBHD activity-stream plugin registered");

  // Narrate each tool the agent reaches for. FAIL-CLOSED hook → fire-and-forget,
  // never throw, always return undefined (never block a real tool call).
  api.on("before_tool_call", (event) => {
    try {
      const p = phaseForEvent(event);
      if (p) void postProgress(p.phase, p.detail, api);
    } catch (_ignored) {
      // never let narration break a tool call
    }
    return undefined;
  });

  // The agent is wrapping up → "composing". Return undefined so we never interfere
  // with the output-guard plugin's revise/finalize decision.
  api.on("before_agent_finalize", () => {
    try {
      void postProgress("composing", "", api);
    } catch (_ignored) {
      // no-op
    }
    return undefined;
  });
}
