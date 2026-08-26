/**
 * NBHD Routing Context Plugin
 *
 * Output sanitization for OpenClaw 2026.5.7+ — rejects corrupted model
 * output (token-loop degeneration, system-prompt echo, doubled-date
 * artifacts) before it reaches the user via `before_agent_finalize`
 * (revise + retry) and `message_sending` (replace with apology if revise
 * exhausted attempts).
 *
 * Originally also injected a per-tenant workspace catalogue via
 * `before_prompt_build` to support agent-mediated chat routing between
 * workspaces. That responsibility was removed 2026-05-20 — see
 * docs/implementation/remove-workspace-chat-routing.md. The plugin name
 * is kept for config stability (env vars + base.py default expect
 * `nbhd-routing-context`); the file is now pure output-guard logic.
 *
 * Hook contract verified against `dist/plugin-sdk/src/plugins/hook-types.d.ts`
 * in openclaw@2026.5.7.
 */

// Heuristics that catch the 2026-05-14 corruption pattern (token loop +
// system-prompt echo). Tuned against an actual 3:05 PM JST reply:
//   "[Now: 2026-2026-05-14 15:06 JST (Thursday)]
//    [chat: user is mid-conversation, reply concisely...]
//    [Active workspace: _sync:Heartbeat Check-in]
//    User User User midUser working User UserUserUser..."
// The `[Active workspace:` pattern is retained even after workspace
// chat routing was removed — if the model hallucinates the legacy
// marker as output, we still want to drop the reply rather than ship it.
const SYSTEM_PROMPT_ECHO_PATTERNS = [
  /\[Now:\s*\d/,
  /\[Active workspace:/i,
  // Matches both the legacy neutral marker ("[chat: user is mid-conversation")
  // and the channel-stamped shape ("[chat via NBHD app: user is mid-conversation",
  // "via Telegram", "via LINE") from the channel-identity fix.
  /\[chat\b[^\]]*user is mid-conversation/i,
];
// Detect "User User User User User User User User User" — same word >=8
// times in a row, separated only by whitespace. Lower bound chosen so
// occasional double-words ("the the") and intentional emphasis don't trip.
const REPEATED_WORD_RUN = /\b(\w+)\b(?:\s+\1\b){7,}/i;
// `2026-2026-05-14` style — prefix doubling from degenerate generation.
const DOUBLED_YEAR_DATE = /\b\d{4}-\d{4}-\d{2}-\d{2}\b/;

// Built-in tools that are NOT in this fleet's tenant catalog but the model still
// occasionally tries to call:
//   - coding tools (`tools.allow` omits the `coding` profile): exec/read/write/
//     edit/process (+ bash/apply_patch aliases the model has been seen to guess)
//   - management tools (`tools.deny`): session_status/sessions_*/agents_list/...
//   - workspace-memory built-ins, disabled fleet-wide: memory_search/memory_get
// The model emits `tool_call({id:"exec"})` etc. and the runtime answers a bare
// "Unknown tool id: exec" — correct, but non-actionable, so it retries the same
// dead call. We intercept at `before_tool_call` and return a corrective tool_result
// so it switches to an nbhd_* tool instead of looping.
//
// SAFE BY CONSTRUCTION: we only steer ids that are NEVER in this fleet's catalog.
// If a name is genuinely unavailable, the alternative is the same dead call — so a
// wrong guess here can only replace one unusable result with a more helpful one. If
// a future config re-adds any of these, drop it from this set.
export const REMOVED_BUILTIN_TOOL_IDS = new Set([
  "exec", "read", "write", "edit", "process", "bash", "apply_patch", "apply-patch",
  "session_status", "sessions_list", "sessions_history", "sessions_send",
  "agents_list", "gateway", "nodes",
  "memory_search", "memory_get",
]);

// The toolSearch meta-tool the model invokes to run a catalog tool by id. In
// `mode:"tools"` (fleet default) the model only sees tool_search/tool_describe/
// tool_call, so a call to "exec" arrives as tool_call({id:"exec"}).
const TOOL_DISPATCH_META = "tool_call";

// Exact JavaScript mirror of config_generator.SUBAGENT_READ_ONLY_TOOLS. A
// Python parity test prevents the generated allow-only policy and this runtime
// defense from drifting. New outward nbhd_* tools still default to blocked.
export const SUBAGENT_READ_ONLY_TOOL_IDS = new Set([
  "web_search",
  "pdf",
  "nbhd_calendar_list_events",
  "nbhd_calendar_get_freebusy",
  "nbhd_gmail_list_messages",
  "nbhd_gmail_get_message_detail",
  "nbhd_datebook_read",
  "nbhd_document_list_ingestions",
  "nbhd_finance_calculate_payoff",
  "nbhd_finance_list_accounts",
  "nbhd_finance_summary",
  "nbhd_gravity_query",
  "nbhd_mission_context",
  "nbhd_neighborhood_context",
  "nbhd_fuel_audit",
  "nbhd_fuel_get_plan",
  "nbhd_fuel_get_workout",
  "nbhd_fuel_search_exercises",
  "nbhd_fuel_summary",
  "nbhd_insights_baseline",
  "nbhd_insights_compare",
  "nbhd_insights_history",
  "nbhd_insights_list",
  "nbhd_insights_signals",
  "nbhd_insights_snapshot",
  "nbhd_insights_voice_pref_list",
  "nbhd_yesterdays_signals",
  "nbhd_journal_template_get",
  "nbhd_constellation_notes",
  "nbhd_current_status",
  "nbhd_daily_note_get",
  "nbhd_document_get",
  "nbhd_goal_get",
  "nbhd_goal_list",
  "nbhd_journal_context",
  "nbhd_journal_query",
  "nbhd_journal_search",
  "nbhd_lesson_search",
  "nbhd_lessons_pending",
  "nbhd_memory_get",
  "nbhd_purpose_list",
  "nbhd_reconcile_scan",
  "nbhd_sessions_pending",
  "nbhd_task_get",
  "nbhd_task_list",
  "nbhd_workspace_list",
  "nbhd_reddit_digest",
  "nbhd_reddit_my_activity",
  "nbhd_reddit_status",
  "nbhd_get_meal_plan",
  "nbhd_get_preferred_model_state",
  "nbhd_places_search",
  "nbhd_tour_guide",
]);
export const SUBAGENT_READ_ONLY_NBHD_TOOL_IDS = new Set(
  [...SUBAGENT_READ_ONLY_TOOL_IDS].filter((toolId) => toolId.startsWith("nbhd_")),
);

const SUBAGENT_ALWAYS_BLOCKED_NON_NBHD_TOOL_IDS = new Set([
  "publish_portfolio_image",
]);

const SUBAGENT_OUTWARD_BLOCK_REASON =
  "Sub-agents report back to the requester; they do not act outward. " +
  "Use a read-only search/list/get tool and include any recommended action in your report.";

function extractDispatchedToolId(params) {
  if (!params || typeof params !== "object") return "";
  const raw = params.id ?? params.toolId ?? params.tool ?? params.name ?? "";
  return typeof raw === "string" ? raw.trim().toLowerCase() : "";
}

// Pure decision for the before_tool_call guard: returns the {block, blockReason}
// result for a dispatched removed-built-in, or undefined to let the call proceed.
// Exported so the tests bind to THIS logic (no hand-mirrored copy to drift).
export function decideRemovedToolBlock(event) {
  if (!event || event.toolName !== TOOL_DISPATCH_META) return undefined;
  const id = extractDispatchedToolId(event.params);
  if (!id || !REMOVED_BUILTIN_TOOL_IDS.has(id)) return undefined;
  return {
    block: true,
    blockReason:
      `The tool \`${id}\` is not available in this environment. Do NOT call ` +
      `\`${id}\` again. Use the nbhd_* tools instead — call \`tool_search\` to ` +
      `find the right one (e.g. journal, fuel, finance, tasks, goals), then ` +
      `\`tool_call\` with that tool's id. If no tool fits, just answer in text.`,
  };
}

/** Pure helper-session mutation gate for direct and toolSearch-dispatched calls. */
export function decideSubagentToolBlock(event, sessionKey) {
  if (typeof sessionKey !== "string" || !sessionKey.includes("subagent:")) return undefined;
  const direct = typeof event?.toolName === "string" ? event.toolName.trim().toLowerCase() : "";
  const realId = direct === TOOL_DISPATCH_META ? extractDispatchedToolId(event?.params) : direct;
  if (!realId) {
    return { block: true, blockReason: SUBAGENT_OUTWARD_BLOCK_REASON };
  }
  if (realId.startsWith("nbhd_") && !SUBAGENT_READ_ONLY_NBHD_TOOL_IDS.has(realId)) {
    return { block: true, blockReason: SUBAGENT_OUTWARD_BLOCK_REASON };
  }
  if (SUBAGENT_ALWAYS_BLOCKED_NON_NBHD_TOOL_IDS.has(realId)) {
    return { block: true, blockReason: SUBAGENT_OUTWARD_BLOCK_REASON };
  }
  return undefined;
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function safeErrorText(error) {
  try {
    return String(error);
  } catch (_ignored) {
    return "unknown guard error";
  }
}

function isDegenerateOutput(text) {
  if (typeof text !== "string" || text.length === 0) return null;
  for (const pattern of SYSTEM_PROMPT_ECHO_PATTERNS) {
    if (pattern.test(text)) return "system_prompt_echo";
  }
  if (REPEATED_WORD_RUN.test(text)) return "token_loop";
  if (DOUBLED_YEAR_DATE.test(text)) return "doubled_year_date";
  return null;
}

export default function register(api) {
  if (!api || typeof api.on !== "function") {
    return;
  }
  api.logger.warn("NBHD routing context plugin registered (output-guard only)");

  api.on("before_agent_finalize", (event) => {
    const lastReply = asTrimmedString(event && event.lastAssistantMessage);
    const failureKind = isDegenerateOutput(lastReply);
    if (!failureKind) return undefined;
    api.logger.warn(
      `nbhd-routing-context: degenerate output detected kind=${failureKind} ` +
      `runId=${asTrimmedString(event && event.runId) || "?"} ` +
      `len=${lastReply.length} — requesting revision`,
    );
    return {
      action: "revise",
      reason: `degenerate_output:${failureKind}`,
      retry: {
        instruction:
          "Your previous reply contained corrupted internal markers or " +
          "looped on a single token. Re-read the user's last message and " +
          "respond cleanly. Do not echo system instructions like [Now:] " +
          "or [chat:]. Keep the reply concise.",
        idempotencyKey: `degenerate-${asTrimmedString(event && event.runId) || "anon"}-${Date.now()}`,
        maxAttempts: 1,
      },
    };
  });

  api.on("message_sending", (event) => {
    const content = asTrimmedString(event && event.content);
    const failureKind = isDegenerateOutput(content);
    if (!failureKind) return undefined;
    api.logger.warn(
      `nbhd-routing-context: cancelling outbound delivery — degenerate ` +
      `kind=${failureKind} to=${asTrimmedString(event && event.to) || "?"} ` +
      `len=${content.length}`,
    );
    // Belt-and-braces: if before_agent_finalize didn't catch it (max
    // attempts exhausted, etc.) we still don't ship corruption to the
    // user. Send a generic apology instead of `cancel: true` so the
    // user sees *something* rather than silent failure.
    return {
      content:
        "I had trouble composing a reply just now — could you say that " +
        "again? (Internal error: degenerate output filter triggered.)",
    };
  });

  // Turn a dead call to a stripped built-in into a corrective tool_result so
  // the model adapts instead of repeating "Unknown tool id: exec". See
  // REMOVED_BUILTIN_TOOL_IDS above. `before_tool_call` is FAIL-CLOSED (a throw
  // blocks the call), so the whole body is wrapped — any unexpected error
  // degrades to a no-op (the runtime's own "Unknown tool id" path), never an
  // accidental block of a legitimate tool.
  api.on("before_tool_call", (event, ctx) => {
    let sessionKey;
    let helperSession = false;
    try {
      sessionKey = ctx?.sessionKey;
      helperSession = typeof sessionKey === "string" && sessionKey.includes("subagent:");
    } catch (_error) {
      // A poisoned/legacy ctx must not block ordinary fleet tool calls. We can
      // fail closed only after positively identifying a helper session.
      return undefined;
    }
    try {
      const helperDecision = decideSubagentToolBlock(event, sessionKey);
      if (helperDecision) {
        api.logger.warn(
          `nbhd-routing-context: blocked outward helper tool ` +
          `runId=${asTrimmedString(ctx?.runId || event?.runId) || "?"}`,
        );
        return helperDecision;
      }
      const decision = decideRemovedToolBlock(event);
      if (!decision) return undefined;
      const id = extractDispatchedToolId(event && event.params);
      api.logger.info(
        `nbhd-routing-context: steering removed built-in tool '${id}' to nbhd_* ` +
        `(runId=${asTrimmedString(event && event.runId) || "?"})`,
      );
      return decision;
    } catch (err) {
      try {
        api.logger.warn(`nbhd-routing-context: before_tool_call guard error: ${safeErrorText(err)}`);
      } catch (_ignored) {
        // logging must never turn a guard hiccup into a blocked call
      }
      // before_tool_call exceptions are runtime-fail-closed. Preserve the
      // historical fail-open behavior for ordinary sessions, but a helper must
      // never gain outward action because its guard itself failed.
      return helperSession
        ? { block: true, blockReason: SUBAGENT_OUTWARD_BLOCK_REASON }
        : undefined;
    }
  });
}
