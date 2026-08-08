/**
 * Unit tests for nbhd-cron-enforcement.
 *
 * Run with: node --test runtime/openclaw/plugins/nbhd-cron-enforcement/test.js
 *
 * Importing index.js is side-effect-free (it only defines functions + exports;
 * the hooks register only when register(api) is called).
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import register, {
  parseContract,
  evaluateCheck,
  decideGuardAction,
  extractSendMessage,
} from "./index.js";

const PREFIX = "nbhd.v1 ";

function contract(check, onFail, pattern = "pure_reminder") {
  return PREFIX + JSON.stringify({ v: 1, pattern, check, on_fail: onFail });
}

// ── parseContract ────────────────────────────────────────────────────────────

describe("parseContract", () => {
  it("parses a well-formed nbhd.v1 contract", () => {
    const raw = contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" });
    const parsed = parseContract(raw);
    assert.equal(parsed.v, 1);
    assert.equal(parsed.pattern, "pure_reminder");
    assert.deepEqual(parsed.check, { kind: "contains", text: "hi" });
    assert.deepEqual(parsed.on_fail, { action: "rewrite", content: "hi" });
  });

  it("returns null for missing/wrong prefix", () => {
    assert.equal(parseContract(""), null);
    assert.equal(parseContract(undefined), null);
    assert.equal(parseContract(null), null);
    assert.equal(parseContract('{"v":1}'), null);
    assert.equal(parseContract("nbhd.v2 {}"), null);
  });

  it("returns null for malformed JSON (fail-open, not a throw)", () => {
    assert.doesNotThrow(() => parseContract("nbhd.v1 {not json"));
    assert.equal(parseContract("nbhd.v1 {not json"), null);
  });

  it("returns null for a JSON array or scalar (not an object envelope)", () => {
    assert.equal(parseContract("nbhd.v1 [1,2,3]"), null);
    assert.equal(parseContract("nbhd.v1 42"), null);
    assert.equal(parseContract('nbhd.v1 "just a string"'), null);
  });

  it("returns null for the wrong version", () => {
    assert.equal(parseContract(PREFIX + JSON.stringify({ v: 2, check: {}, on_fail: {} })), null);
  });

  it("tolerates a contract missing check/on_fail (defers to decideGuardAction)", () => {
    const parsed = parseContract(PREFIX + JSON.stringify({ v: 1, pattern: "x" }));
    assert.ok(parsed);
    assert.equal(parsed.check, undefined);
  });
});

// ── evaluateCheck ────────────────────────────────────────────────────────────

describe("evaluateCheck", () => {
  it("contains: exact match and substring both pass", () => {
    assert.equal(evaluateCheck({ kind: "contains", text: "Take out trash" }, "Take out trash"), true);
    assert.equal(
      evaluateCheck({ kind: "contains", text: "Take out trash" }, 'Reminder: "Take out trash" today!'),
      true,
    );
  });

  it("contains: drift fails", () => {
    assert.equal(evaluateCheck({ kind: "contains", text: "Take out trash" }, "Don't forget your chores"), false);
  });

  it("contains: compares against TRIMMED content", () => {
    assert.equal(evaluateCheck({ kind: "contains", text: "hi" }, "  hi  "), true);
  });

  it("contains: an empty expected text is vacuously true", () => {
    assert.equal(evaluateCheck({ kind: "contains", text: "" }, "anything"), true);
  });

  it("marker: present on the first line passes, raw (untrimmed) content", () => {
    assert.equal(evaluateCheck({ kind: "marker", marker: "[block: x]" }, "[block: x]\nbody"), true);
  });

  it("marker: absent fails", () => {
    assert.equal(evaluateCheck({ kind: "marker", marker: "[block: x]" }, "no marker here"), false);
  });

  it("bounded: non-empty and within max passes", () => {
    assert.equal(evaluateCheck({ kind: "bounded", max: 800 }, "Great session!"), true);
  });

  it("bounded: empty (even whitespace-only) fails", () => {
    assert.equal(evaluateCheck({ kind: "bounded", max: 800 }, "   "), false);
    assert.equal(evaluateCheck({ kind: "bounded", max: 800 }, ""), false);
  });

  it("bounded: over max fails", () => {
    assert.equal(evaluateCheck({ kind: "bounded", max: 800 }, "x".repeat(900)), false);
  });

  it("bounded: counts Unicode CODE POINTS, not UTF-16 code units (regression)", () => {
    // 400 ASCII + 250 astral emoji (U+1F4AA, outside the BMP) = 650 code
    // points (Python's len(str)) but 900 UTF-16 code units (naive .length,
    // since each astral char is a surrogate pair). Must pass at max:800.
    const content = "x".repeat(400) + "\u{1F4AA}".repeat(250);
    assert.equal(content.length, 900, "sanity: raw UTF-16 length is 900");
    assert.equal([...content].length, 650, "sanity: code-point length is 650");
    assert.equal(evaluateCheck({ kind: "bounded", max: 800 }, content), true);
  });

  it("fail-open on garbage check input — never throws, always passes", () => {
    assert.doesNotThrow(() => evaluateCheck(null, "content"));
    assert.equal(evaluateCheck(null, "content"), true);
    assert.equal(evaluateCheck(undefined, "content"), true);
    assert.equal(evaluateCheck("not an object", "content"), true);
    assert.equal(evaluateCheck({ kind: "unknown_kind" }, "content"), true);
    // Wrong-typed leaf value (number instead of string) must not throw.
    assert.doesNotThrow(() => evaluateCheck({ kind: "contains", text: 12345 }, "content"));
  });

  it("fail-open on garbage content input — never throws", () => {
    assert.doesNotThrow(() => evaluateCheck({ kind: "contains", text: "hi" }, null));
    assert.doesNotThrow(() => evaluateCheck({ kind: "contains", text: "hi" }, undefined));
    assert.doesNotThrow(() => evaluateCheck({ kind: "contains", text: "hi" }, 42));
  });
});

// ── decideGuardAction ────────────────────────────────────────────────────────

describe("decideGuardAction", () => {
  it("passes through when the check is satisfied", () => {
    const c = { check: { kind: "contains", text: "hi" }, on_fail: { action: "rewrite", content: "hi" } };
    assert.deepEqual(decideGuardAction(c, "hi there", 0), { type: "pass" });
  });

  it("rewrite: fails straight to rewrite content, no revision budget consulted", () => {
    const c = { check: { kind: "contains", text: "hi" }, on_fail: { action: "rewrite", content: "hi" } };
    assert.deepEqual(decideGuardAction(c, "wrong", 0), { type: "rewrite", content: "hi" });
  });

  it("revise_then_rewrite: revises while under budget", () => {
    const c = {
      check: { kind: "contains", text: "hi" },
      on_fail: { action: "revise_then_rewrite", content: "hi", max_revisions: 1 },
    };
    const decision = decideGuardAction(c, "wrong", 0);
    assert.equal(decision.type, "revise");
    assert.match(decision.reason, /verbatim text/);
  });

  it("revise_then_rewrite: rewrites once the budget is exhausted", () => {
    const c = {
      check: { kind: "contains", text: "hi" },
      on_fail: { action: "revise_then_rewrite", content: "hi", max_revisions: 1 },
    };
    assert.deepEqual(decideGuardAction(c, "wrong", 1), { type: "rewrite", content: "hi" });
    assert.deepEqual(decideGuardAction(c, "wrong", 2), { type: "rewrite", content: "hi" });
  });

  it("revise_then_allow: revises while under budget, then allows through", () => {
    const c = {
      check: { kind: "marker", marker: "[block: x]" },
      on_fail: { action: "revise_then_allow", max_revisions: 1 },
    };
    assert.equal(decideGuardAction(c, "no marker", 0).type, "revise");
    assert.deepEqual(decideGuardAction(c, "no marker", 1), { type: "allow" });
  });

  it("unknown on_fail.action fails open (pass)", () => {
    const c = { check: { kind: "contains", text: "hi" }, on_fail: { action: "made_up_action" } };
    assert.deepEqual(decideGuardAction(c, "wrong", 0), { type: "pass" });
  });

  it("missing/garbage on_fail fails open — never throws", () => {
    assert.doesNotThrow(() => decideGuardAction(null, "wrong", 0));
    assert.deepEqual(decideGuardAction(null, "wrong", 0), { type: "pass" });
    assert.deepEqual(decideGuardAction({ check: { kind: "contains", text: "hi" } }, "wrong", 0), { type: "pass" });
    assert.deepEqual(decideGuardAction({ check: { kind: "contains", text: "hi" }, on_fail: null }, "wrong", 0), {
      type: "pass",
    });
  });

  it("garbage revisionsUsed is treated as 0 — never throws, never NaN-compares true", () => {
    const c = {
      check: { kind: "contains", text: "hi" },
      on_fail: { action: "revise_then_rewrite", content: "hi", max_revisions: 1 },
    };
    assert.doesNotThrow(() => decideGuardAction(c, "wrong", undefined));
    assert.equal(decideGuardAction(c, "wrong", undefined).type, "revise");
    assert.doesNotThrow(() => decideGuardAction(c, "wrong", "not a number"));
  });
});

// ── extractSendMessage (dispatch-shape handling) ────────────────────────────

describe("extractSendMessage", () => {
  it("matches a direct dispatch", () => {
    const out = extractSendMessage({ toolName: "nbhd_send_to_user", params: { message: "hi", job_name: "Trash" } });
    assert.deepEqual(out, { matched: true, shape: "direct", nestedKey: null, message: "hi" });
  });

  it("matches a toolSearch meta-dispatch with nested args under params.params", () => {
    const out = extractSendMessage({
      toolName: "tool_call",
      params: { id: "nbhd_send_to_user", params: { message: "hi" } },
    });
    assert.deepEqual(out, { matched: true, shape: "meta", nestedKey: "params", message: "hi" });
  });

  it("matches a toolSearch meta-dispatch with nested args under params.arguments", () => {
    const out = extractSendMessage({
      toolName: "tool_call",
      params: { id: "nbhd_send_to_user", arguments: { message: "hi" } },
    });
    assert.deepEqual(out, { matched: true, shape: "meta", nestedKey: "arguments", message: "hi" });
  });

  it("meta-dispatch is case/whitespace tolerant on the id, like nbhd-routing-context", () => {
    const out = extractSendMessage({
      toolName: "tool_call",
      params: { id: "  NBHD_SEND_TO_USER ", params: { message: "hi" } },
    });
    assert.equal(out.matched, true);
  });

  it("does NOT match a meta-dispatch to a different tool", () => {
    assert.deepEqual(extractSendMessage({ toolName: "tool_call", params: { id: "nbhd_task_create" } }), {
      matched: false,
    });
  });

  it("does NOT match a direct dispatch of a different tool", () => {
    assert.deepEqual(extractSendMessage({ toolName: "nbhd_task_create", params: {} }), { matched: false });
  });

  it("does NOT match a meta-dispatch with no recognizable nested-args shape", () => {
    assert.deepEqual(extractSendMessage({ toolName: "tool_call", params: { id: "nbhd_send_to_user" } }), {
      matched: false,
    });
  });

  it("tolerates garbage/missing event shapes without throwing", () => {
    assert.doesNotThrow(() => extractSendMessage(null));
    assert.doesNotThrow(() => extractSendMessage(undefined));
    assert.doesNotThrow(() => extractSendMessage({}));
    assert.deepEqual(extractSendMessage(null), { matched: false });
    assert.deepEqual(extractSendMessage({ toolName: "tool_call", params: null }), { matched: false });
  });
});

// ── PARITY CASE-TABLE ────────────────────────────────────────────────────────
// The JS twin of apps/cron/tests/test_patterns.py::PARITY_CASES. Every case
// there has an IDENTICAL twin here, expressed against the generic
// {kind, ...} check dict (evaluateCheck) instead of a pattern + Pydantic
// payload (validate_outbound_message). Kept in sync BY HAND — that manual
// duplication is the drift control between the two languages. If you change
// one side's pass/fail semantics, update both tables.
//
// Tuple shape: [check, content, expectedPass]
const PARITY_CASES = [
  // ── pure_reminder / quote_user_intent: contains ─────────────────────────
  [{ kind: "contains", text: "Take out trash" }, "Take out trash", true],
  [{ kind: "contains", text: "Take out trash" }, 'Reminder: "Take out trash" today!', true],
  [{ kind: "contains", text: "Take out trash" }, "Don't forget your chores", false],
  [{ kind: "contains", text: "appointment Tuesday 3pm" }, 'Heads up — "appointment Tuesday 3pm" is coming up!', true],
  [{ kind: "contains", text: "appointment Tuesday 3pm" }, "Something is happening this week", false],
  // ── domain_summary / daily_briefing: marker ─────────────────────────────
  [{ kind: "marker", marker: "[block: task_summary]" }, "[block: task_summary]\n- 3 open tasks", true],
  [{ kind: "marker", marker: "[block: task_summary]" }, "You have 3 open tasks", false],
  [{ kind: "marker", marker: "[block: daily_briefing]" }, "[block: daily_briefing]\nGood morning!", true],
  [{ kind: "marker", marker: "[block: daily_briefing]" }, "Good morning! Your day looks busy.", false],
  // ── task_hygiene: marker ─────────────────────────────────────────────────
  [{ kind: "marker", marker: "[block: task_hygiene]" }, "[block: task_hygiene]\nClosed 2, deferred 1.", true],
  [{ kind: "marker", marker: "[block: task_hygiene]" }, "I tidied up your task list this week.", false],
  // ── workout_congrats: bounded ────────────────────────────────────────────
  [{ kind: "bounded", max: 800 }, "Great push session — third this week!", true],
  [{ kind: "bounded", max: 800 }, "   ", false],
  [{ kind: "bounded", max: 800 }, "x".repeat(900), false],
  // Code-point vs UTF-16-code-unit drift regression (Fable review, round 2):
  // 400 ASCII + 250 astral emoji = 650 code points (Python's len(str)) but
  // 900 UTF-16 code units — must pass, matching the Python twin case.
  [{ kind: "bounded", max: 800 }, "x".repeat(400) + "\u{1F4AA}".repeat(250), true],
];

describe("PARITY_CASES (JS side — twin of the Python PARITY_CASES table)", () => {
  it("evaluateCheck matches the expected verdict for every case", () => {
    for (const [check, content, expectedPass] of PARITY_CASES) {
      assert.equal(evaluateCheck(check, content), expectedPass, `check=${JSON.stringify(check)} content=${content}`);
    }
  });
});

// ── register() end-to-end wiring ─────────────────────────────────────────────

function makeFakeApi(pluginConfig) {
  const handlers = {};
  return {
    on: (event, fn) => {
      handlers[event] = fn;
    },
    logger: { info() {}, warn() {}, error() {} },
    pluginConfig: pluginConfig || {},
    _handlers: handlers,
  };
}

describe("register() wires up cron_changed + before_tool_call", () => {
  it("registers both hooks", () => {
    const api = makeFakeApi();
    register(api);
    assert.equal(typeof api._handlers["cron_changed"], "function");
    assert.equal(typeof api._handlers["before_tool_call"], "function");
  });

  it("does nothing (no throw) when api.on is absent", () => {
    assert.doesNotThrow(() => register({}));
    assert.doesNotThrow(() => register(null));
  });

  it("a non-cron session (no cached entry) is a cache-miss no-op", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];
    const result = beforeToolCall({
      sessionKey: "some-chat-session",
      toolName: "nbhd_send_to_user",
      params: { message: "anything" },
    });
    assert.equal(result, undefined);
  });

  it("a cron with no contract (freeform/legacy) caches nothing — before_tool_call no-ops", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({ action: "started", sessionKey: "sess-freeform", job: { name: "freeform-job" } }); // no description
    const result = beforeToolCall({
      sessionKey: "sess-freeform",
      toolName: "nbhd_send_to_user",
      params: { message: "anything goes" },
    });
    assert.equal(result, undefined);
  });

  it("end-to-end: rewrite action swaps the message, direct dispatch shape", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-1",
      job: {
        name: "hydrate",
        description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }),
      },
    });

    const result = beforeToolCall({
      sessionKey: "sess-1",
      toolName: "nbhd_send_to_user",
      params: { message: "You should stay hydrated", job_name: "hydrate" },
    });
    assert.deepEqual(result, { params: { message: "Drink water", job_name: "hydrate" } });
  });

  it("end-to-end: rewrite action swaps the message, toolSearch meta dispatch shape (params.params)", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-2",
      job: {
        name: "hydrate",
        description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }),
      },
    });

    const result = beforeToolCall({
      sessionKey: "sess-2",
      toolName: "tool_call",
      params: { id: "nbhd_send_to_user", params: { message: "wrong text", job_name: "hydrate" } },
    });
    assert.deepEqual(result, { params: { id: "nbhd_send_to_user", params: { message: "Drink water", job_name: "hydrate" } } });
  });

  it("end-to-end: rewrite action swaps the message, toolSearch meta dispatch shape (params.arguments)", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-3",
      job: { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) },
    });

    const result = beforeToolCall({
      sessionKey: "sess-3",
      toolName: "tool_call",
      params: { id: "nbhd_send_to_user", arguments: { message: "wrong text" } },
    });
    assert.deepEqual(result, { params: { id: "nbhd_send_to_user", arguments: { message: "Drink water" } } });
  });

  it("end-to-end: revise_then_rewrite blocks under budget, then rewrites once exhausted", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-4",
      job: {
        name: "appt",
        description: contract(
          { kind: "contains", text: "appointment Tuesday 3pm" },
          { action: "revise_then_rewrite", content: "appointment Tuesday 3pm", max_revisions: 1 },
          "quote_user_intent",
        ),
      },
    });

    const badCall = { sessionKey: "sess-4", toolName: "nbhd_send_to_user", params: { message: "no verbatim text" } };
    const first = beforeToolCall(badCall);
    assert.equal(first.block, true);
    assert.match(first.blockReason, /verbatim text/);

    // Budget exhausted (1 revision used) — next attempt rewrites instead of blocking again.
    const second = beforeToolCall(badCall);
    assert.deepEqual(second, { params: { message: "appointment Tuesday 3pm" } });
  });

  it("end-to-end: revise_then_allow blocks under budget, then allows through once exhausted", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-5",
      job: {
        name: "weekly-tasks",
        description: contract(
          { kind: "marker", marker: "[block: task_summary]" },
          { action: "revise_then_allow", max_revisions: 1 },
          "domain_summary",
        ),
      },
    });

    const badCall = { sessionKey: "sess-5", toolName: "nbhd_send_to_user", params: { message: "no marker here" } };
    assert.equal(beforeToolCall(badCall).block, true);
    // Budget exhausted — ships as-is (undefined = no interference with the call).
    assert.equal(beforeToolCall(badCall), undefined);
  });

  it("a passing message is never touched", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-6",
      job: { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) },
    });

    const result = beforeToolCall({
      sessionKey: "sess-6",
      toolName: "nbhd_send_to_user",
      params: { message: "Drink water" },
    });
    assert.equal(result, undefined);
  });

  it("finished/removed clears the cache for both sessionKey and runId", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-7",
      runId: "run-7",
      job: { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) },
    });
    cronChanged({ action: "finished", sessionKey: "sess-7", runId: "run-7" });

    assert.equal(
      beforeToolCall({ sessionKey: "sess-7", toolName: "nbhd_send_to_user", params: { message: "bad" } }),
      undefined,
    );
    assert.equal(
      beforeToolCall({ runId: "run-7", toolName: "nbhd_send_to_user", params: { message: "bad" } }),
      undefined,
    );
  });

  it("caches under both sessionKey AND runId — before_tool_call finds it via either", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "sess-8",
      runId: "run-8",
      job: { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) },
    });

    // Only runId present on this before_tool_call event — must still hit.
    const result = beforeToolCall({ runId: "run-8", toolName: "nbhd_send_to_user", params: { message: "wrong" } });
    assert.deepEqual(result, { params: { message: "Drink water" } });
  });

  it("prunes expired cache entries on the next cron_changed call", () => {
    const api = makeFakeApi({ cacheTtlSeconds: 60 });
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    const realNow = Date.now;
    try {
      let now = 1_000_000_000;
      Date.now = () => now;

      cronChanged({
        action: "started",
        sessionKey: "sess-old",
        job: { name: "old", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) },
      });
      assert.equal(
        beforeToolCall({ sessionKey: "sess-old", toolName: "nbhd_send_to_user", params: { message: "bad" } })?.params
          ?.message,
        "hi",
      );

      now += 61_000; // past the 60s ttl
      cronChanged({
        action: "started",
        sessionKey: "sess-new",
        job: { name: "new", description: contract({ kind: "contains", text: "yo" }, { action: "rewrite", content: "yo" }) },
      });

      // sess-old's entry should have been pruned by the pruneExpired() call at
      // the top of this second cron_changed — before_tool_call now sees a miss.
      assert.equal(
        beforeToolCall({ sessionKey: "sess-old", toolName: "nbhd_send_to_user", params: { message: "bad" } }),
        undefined,
      );
    } finally {
      Date.now = realNow;
    }
  });
});

// ── throw-safety (poisoned cache / garbage events) ──────────────────────────
// The spec's core risk: before_tool_call is FAIL-CLOSED at the runtime level,
// so a throw here would block EVERY tool call fleet-wide once enabled. These
// tests poison the cached contract (valid JSON, garbage semantics) and feed
// garbage event shapes, and assert the hook NEVER throws — only ever
// undefined or a well-formed decision.

describe("before_tool_call throw-safety", () => {
  it("a structurally poisoned contract (check=null, wrong-typed leaves) never throws", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    const poisoned =
      PREFIX + JSON.stringify({ v: 1, pattern: "x", check: null, on_fail: { action: "rewrite", content: 12345 } });
    cronChanged({ action: "started", sessionKey: "poison-1", job: { name: "p", description: poisoned } });

    assert.doesNotThrow(() =>
      beforeToolCall({ sessionKey: "poison-1", toolName: "nbhd_send_to_user", params: { message: "hi" } }),
    );
  });

  it("a contract with a garbage on_fail.action never throws", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    const poisoned = PREFIX + JSON.stringify({ v: 1, pattern: "x", check: { kind: "bogus" }, on_fail: 42 });
    cronChanged({ action: "started", sessionKey: "poison-2", job: { name: "p2", description: poisoned } });

    assert.doesNotThrow(() =>
      beforeToolCall({ sessionKey: "poison-2", toolName: "nbhd_send_to_user", params: { message: "hi" } }),
    );
  });

  it("a thrown Symbol reaching the catch block never escapes (safeLogError regression)", () => {
    // A Symbol thrown from inside the try body ends up as `error` in `catch
    // (error)`. Naively interpolating it into a template literal for logging
    // (`${error}`) throws TypeError: Cannot convert a Symbol value to a
    // string — which, done outside a guard, would escape THIS catch block
    // and blow past the fail-open guarantee. Force that path by making a
    // property read inside the hook's try body throw a Symbol.
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    const poisonedEvent = { toolName: "nbhd_send_to_user", params: { message: "hi" } };
    Object.defineProperty(poisonedEvent, "sessionKey", {
      get() {
        throw Symbol("boom");
      },
    });

    assert.doesNotThrow(() => beforeToolCall(poisonedEvent));
    assert.equal(beforeToolCall(poisonedEvent), undefined);
  });

  it("a caught error with a throwing `.message` getter never escapes (safeLogError regression)", () => {
    // Mirrors the same hazard one level deeper: the THROWN VALUE itself is an
    // object whose `.message` getter throws when read — exactly what
    // safeLogError's `(error && error.message) || error` line touches first.
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];

    const poisonedEvent = { action: "started", job: { name: "p", description: "nbhd.v1 {}" } };
    Object.defineProperty(poisonedEvent, "sessionKey", {
      get() {
        const poisonedError = {};
        Object.defineProperty(poisonedError, "message", {
          get() {
            throw new Error("nested throw from a poisoned .message getter");
          },
        });
        throw poisonedError;
      },
    });

    assert.doesNotThrow(() => cronChanged(poisonedEvent));
    assert.equal(cronChanged(poisonedEvent), undefined);
  });

  it("garbage before_tool_call event shapes never throw, even with a live contract cached", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "poison-3",
      job: { name: "p3", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) },
    });

    for (const garbage of [
      null,
      undefined,
      {},
      { toolName: 42 },
      { toolName: "tool_call", params: null },
      { sessionKey: "poison-3", toolName: "nbhd_send_to_user", params: null },
      { sessionKey: "poison-3", toolName: "tool_call", params: { id: "nbhd_send_to_user", params: "not an object" } },
    ]) {
      assert.doesNotThrow(() => beforeToolCall(garbage));
    }
  });

  it("a throwing logger never blocks the call — undefined path", () => {
    const throwingApi = makeFakeApi();
    throwingApi.logger.warn = () => {
      throw new Error("logger boom");
    };
    register(throwingApi);
    const cronChanged = throwingApi._handlers["cron_changed"];
    const beforeToolCall = throwingApi._handlers["before_tool_call"];

    assert.doesNotThrow(() =>
      cronChanged({ action: "started", sessionKey: "s-log-1", job: { name: "j", description: "nbhd.v1 {not json" } }),
    );

    cronChanged({
      action: "started",
      sessionKey: "s-log-2",
      job: { name: "j2", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) },
    });
    assert.doesNotThrow(() =>
      beforeToolCall({ sessionKey: "s-log-2", toolName: "nbhd_send_to_user", params: { message: "hi" } }),
    );
  });

  it("a throwing logger does not prevent the rewrite decision from taking effect", () => {
    const throwingApi = makeFakeApi();
    throwingApi.logger.warn = () => {
      throw new Error("logger boom");
    };
    register(throwingApi);
    const cronChanged = throwingApi._handlers["cron_changed"];
    const beforeToolCall = throwingApi._handlers["before_tool_call"];

    cronChanged({
      action: "started",
      sessionKey: "s-log-3",
      job: { name: "j3", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) },
    });

    let result;
    assert.doesNotThrow(() => {
      result = beforeToolCall({ sessionKey: "s-log-3", toolName: "nbhd_send_to_user", params: { message: "wrong text" } });
    });
    assert.deepEqual(result, { params: { message: "hi" } });
  });

  it("a throwing logger on cron_changed never propagates", () => {
    const throwingApi = makeFakeApi();
    throwingApi.logger.warn = () => {
      throw new Error("logger boom");
    };
    register(throwingApi);
    const cronChanged = throwingApi._handlers["cron_changed"];
    // Malformed description forces the warn-adjacent path to be exercised;
    // even though parseContract itself doesn't log, this proves cron_changed's
    // own catch-block logging can't escape either.
    assert.doesNotThrow(() =>
      cronChanged({ action: "started", sessionKey: "s-log-4", job: { name: "j4", description: undefined } }),
    );
  });
});
