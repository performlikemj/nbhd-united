/**
 * Unit tests for the degenerate-output heuristic.
 *
 * Run with: node --test runtime/openclaw/plugins/nbhd-routing-context/test.js
 *
 * Fixtures based on the actual 2026-05-14 06:05:13 UTC corrupted reply from
 * canary `oc-148ccf1c-ef13-47f8-a` (the incident that motivated this plugin).
 * See CONTINUITY_workspace-routing-fix.md, Phases 3-4.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

// Mirror of the heuristic from index.js. Kept in sync by hand; the
// canonical copy is index.js. If you change one, change both.
const SYSTEM_PROMPT_ECHO_PATTERNS = [
  /\[Now:\s*\d/,
  /\[Active workspace:/i,
  /\[chat:\s*user is mid-conversation/i,
];
const REPEATED_WORD_RUN = /\b(\w+)\b(?:\s+\1\b){7,}/i;
const DOUBLED_YEAR_DATE = /\b\d{4}-\d{4}-\d{2}-\d{2}\b/;

function isDegenerateOutput(text) {
  if (typeof text !== "string" || text.length === 0) return null;
  for (const pattern of SYSTEM_PROMPT_ECHO_PATTERNS) {
    if (pattern.test(text)) return "system_prompt_echo";
  }
  if (REPEATED_WORD_RUN.test(text)) return "token_loop";
  if (DOUBLED_YEAR_DATE.test(text)) return "doubled_year_date";
  return null;
}

describe("isDegenerateOutput", () => {
  it("returns null for normal prose", () => {
    assert.equal(
      isDegenerateOutput("Here's the chef outreach list — 12 contacted, 4 on hold."),
      null,
    );
  });

  it("returns null for empty/non-string input", () => {
    assert.equal(isDegenerateOutput(""), null);
    assert.equal(isDegenerateOutput(null), null);
    assert.equal(isDegenerateOutput(undefined), null);
    assert.equal(isDegenerateOutput(42), null);
  });

  it("flags [Now: ...] echo", () => {
    assert.equal(
      isDegenerateOutput("[Now: 2026-05-14 15:06 JST (Thursday)]\nHello"),
      "system_prompt_echo",
    );
  });

  it("flags [Active workspace: ...] echo", () => {
    assert.equal(
      isDegenerateOutput("[Active workspace: sautai]\nGot it."),
      "system_prompt_echo",
    );
  });

  it("flags [chat: user is mid-conversation ...] echo", () => {
    assert.equal(
      isDegenerateOutput("[chat: user is mid-conversation, reply concisely without loading]"),
      "system_prompt_echo",
    );
  });

  it("flags the 2026-05-14 token loop", () => {
    const corrupted = "User User User User User User User User User User";
    assert.equal(isDegenerateOutput(corrupted), "token_loop");
  });

  it("flags doubled-year date artifact 2026-2026-05-14", () => {
    assert.equal(
      isDegenerateOutput("Time is 2026-2026-05-14 right now"),
      "doubled_year_date",
    );
  });

  it("flags the full 2026-05-14 canary corruption", () => {
    // Trimmed copy of the actual See more block from the LINE screenshot.
    const corrupted =
      "[Now: 2026-2026-05-14 15:06 JST (Thursday)]\n" +
      "[chat: user is mid-conversation, reply concisely without loading workspace docs]\n" +
      "[Active workspace: _sync:Heartbeat Check-in]\n" +
      "User User User User User User User User User User";
    // First pattern match wins — system_prompt_echo is checked before token_loop.
    assert.equal(isDegenerateOutput(corrupted), "system_prompt_echo");
  });

  it("does NOT flag occasional doubled words ('the the')", () => {
    assert.equal(
      isDegenerateOutput("I went to the the store yesterday — sorry, typo."),
      null,
    );
  });

  it("does NOT flag legitimate dates", () => {
    assert.equal(isDegenerateOutput("Deadline: 2026-05-14"), null);
    assert.equal(isDegenerateOutput("From 2026-05-14 to 2026-05-21"), null);
  });

  it("does NOT flag legitimate emphasis (single repeated word)", () => {
    assert.equal(isDegenerateOutput("yes yes yes — let's do it"), null);
  });

  it("flags exactly 8 word repetitions (boundary)", () => {
    // 1 occurrence + 7 follow-ups = REPEATED_WORD_RUN minimum match
    const text = "go go go go go go go go";
    assert.equal(isDegenerateOutput(text), "token_loop");
  });

  it("does NOT flag 7 word repetitions (below boundary)", () => {
    const text = "go go go go go go go";
    assert.equal(isDegenerateOutput(text), null);
  });
});
