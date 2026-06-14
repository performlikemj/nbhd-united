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
import register, {
  decideRemovedToolBlock,
  REMOVED_BUILTIN_TOOL_IDS,
} from "./index.js";

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

// ── before_tool_call removed-built-in guard ──────────────────────────────────
// These bind to the CANONICAL guard exported by index.js (decideRemovedToolBlock,
// REMOVED_BUILTIN_TOOL_IDS) — no hand-mirrored copy, so the set can't drift out of
// sync with the production code.

describe("decideRemovedToolBlock (canonical guard)", () => {
  it("blocks a dispatched removed built-in (exec) with actionable guidance", () => {
    const out = decideRemovedToolBlock({ toolName: "tool_call", params: { id: "exec" } });
    assert.equal(out?.block, true);
    assert.match(out.blockReason, /`exec`/);
    assert.match(out.blockReason, /nbhd_\*/);
    assert.match(out.blockReason, /Do NOT call/i);
  });

  it("blocks read/write/session_status/process and the disabled memory_search", () => {
    for (const id of ["read", "write", "session_status", "process", "memory_search", "memory_get"]) {
      assert.equal(
        decideRemovedToolBlock({ toolName: "tool_call", params: { id } })?.block,
        true,
        id,
      );
    }
  });

  it("is case- and whitespace-insensitive on the id", () => {
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: { id: "  EXEC " } })?.block, true);
  });

  it("reads alternate id fields (toolId/tool/name)", () => {
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: { toolId: "exec" } })?.block, true);
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: { name: "read" } })?.block, true);
  });

  it("does NOT block a legitimate nbhd_* tool dispatch", () => {
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: { id: "nbhd_task_create" } }), undefined);
  });

  it("does NOT touch direct (non-dispatch) tool calls", () => {
    // A direct nbhd_* call has toolName != "tool_call" — never intercepted.
    assert.equal(decideRemovedToolBlock({ toolName: "nbhd_fuel_summary", params: {} }), undefined);
    // Even a direct call literally named like a removed builtin is left alone here
    // (only the toolSearch dispatch path is guarded).
    assert.equal(decideRemovedToolBlock({ toolName: "exec", params: {} }), undefined);
  });

  it("does NOT block tool_search / tool_describe themselves", () => {
    assert.equal(decideRemovedToolBlock({ toolName: "tool_search", params: { query: "fuel" } }), undefined);
  });

  it("tolerates missing/odd params without blocking", () => {
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call" }), undefined);
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: null }), undefined);
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: { id: 42 } }), undefined);
    assert.equal(decideRemovedToolBlock({ toolName: "tool_call", params: { id: "" } }), undefined);
    assert.equal(decideRemovedToolBlock({}), undefined);
    assert.equal(decideRemovedToolBlock(null), undefined);
  });

  it("the set includes the observed-hallucinated ids", () => {
    for (const id of ["exec", "read", "session_status", "memory_search"]) {
      assert.ok(REMOVED_BUILTIN_TOOL_IDS.has(id), id);
    }
  });
});

// Verify the ACTUAL registration path: drive register() with a fake api, capture
// the before_tool_call handler it registers, and assert it behaves + never throws.
// This catches a wrong hook name or a registration that doesn't wire up — which a
// pure-helper test can't.
describe("register() wires up the before_tool_call guard", () => {
  function makeFakeApi() {
    const handlers = {};
    return {
      on: (event, fn) => {
        handlers[event] = fn;
      },
      logger: { info() {}, warn() {}, error() {} },
      _handlers: handlers,
    };
  }

  it("registers a before_tool_call handler that blocks removed ids", () => {
    const api = makeFakeApi();
    register(api);
    const hook = api._handlers["before_tool_call"];
    assert.equal(typeof hook, "function");
    assert.equal(hook({ toolName: "tool_call", params: { id: "exec" } })?.block, true);
    assert.equal(hook({ toolName: "tool_call", params: { id: "nbhd_task_create" } }), undefined);
  });

  it("the registered handler is fail-safe — never throws, returns undefined on bad input", () => {
    const api = makeFakeApi();
    register(api);
    const hook = api._handlers["before_tool_call"];
    // Normal hook tolerates junk without throwing.
    assert.doesNotThrow(() => hook(null));
    assert.equal(hook(undefined), undefined);

    // If the hook's own logging throws while steering a removed id, the fail-closed
    // try/catch must swallow it and return undefined (let the call proceed to the
    // runtime's own "Unknown tool id"), never propagate a throw that would BLOCK.
    // Throw only on the hook's "steering" log, not the registration log, so
    // register() itself still succeeds.
    const throwingApi = makeFakeApi();
    throwingApi.logger.info = (msg) => {
      if (typeof msg === "string" && msg.includes("steering")) throw new Error("logger boom");
    };
    register(throwingApi);
    const throwingHook = throwingApi._handlers["before_tool_call"];
    assert.equal(typeof throwingHook, "function");
    assert.doesNotThrow(() => throwingHook({ toolName: "tool_call", params: { id: "exec" } }));
    assert.equal(throwingHook({ toolName: "tool_call", params: { id: "exec" } }), undefined);
  });
});
