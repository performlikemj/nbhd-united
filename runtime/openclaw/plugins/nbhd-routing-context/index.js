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
  /\[chat:\s*user is mid-conversation/i,
];
// Detect "User User User User User User User User User" — same word >=8
// times in a row, separated only by whitespace. Lower bound chosen so
// occasional double-words ("the the") and intentional emphasis don't trip.
const REPEATED_WORD_RUN = /\b(\w+)\b(?:\s+\1\b){7,}/i;
// `2026-2026-05-14` style — prefix doubling from degenerate generation.
const DOUBLED_YEAR_DATE = /\b\d{4}-\d{4}-\d{2}-\d{2}\b/;

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
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
  api.logger.info("NBHD routing context plugin registered (output-guard only)");

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
}
