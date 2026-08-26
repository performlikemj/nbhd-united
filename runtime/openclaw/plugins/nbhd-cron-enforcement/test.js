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
import { createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";

import register, {
  parseContract,
  evaluateCheck,
  decideGuardAction,
  decideLimitAction,
  extractSendMessage,
  resolveDispatchedToolId,
} from "./index.js";

const PREFIX = "nbhd.v1 ";

function contract(check, onFail, pattern = "pure_reminder") {
  return PREFIX + JSON.stringify({ v: 1, pattern, check, on_fail: onFail });
}

// The task_hygiene contract exactly as apps/cron/signals.py bakes it — marker
// check plus the fire-time caps. Mirrors
// apps/cron/patterns/task_hygiene.py::get_outbound_contract.
const HYGIENE_MARKER = "[block: task_hygiene]";
function hygieneContract(limits = { sends: 1, mutations: 10 }) {
  return (
    PREFIX +
    JSON.stringify({
      v: 1,
      pattern: "task_hygiene",
      check: { kind: "marker", marker: HYGIENE_MARKER },
      on_fail: { action: "revise_then_allow", max_revisions: 1 },
      limits,
    })
  );
}

function startCron(api, runId, job, jobId = `job-${runId}`) {
  api._handlers["cron_changed"]({
    jobId,
    action: "started",
    job,
    runAtMs: Date.now(),
  });
  const promptResult = api._handlers["before_prompt_build"]({}, {
    trigger: "cron",
    jobId,
    runId,
  });
  assert.equal(promptResult, undefined);
}

function startHygieneCron(runId, limits) {
  const api = makeFakeApi();
  register(api);
  startCron(api, runId, { name: "Task Hygiene", description: hygieneContract(limits) });
  return api._handlers["before_tool_call"];
}

function sendCall(runId, message) {
  return { runId, toolName: "nbhd_send_to_user", params: { message } };
}

function mutationCall(runId, toolName = "nbhd_task_complete") {
  return { runId, toolName, params: { task_id: "t-1" } };
}

// openclaw@2026.5.28 does not replace flat params: it shallow-merges hook
// params over the originals in mergeParamsWithApprovalOverrides
// (agent-tools.before-tool-call-CcOZYWx4.js:515-523, invoked at :1035-1038).
// Keep these assertions on the values the tool actually executes, not merely
// on the hook's return object.
function mergeLikePinnedSdk(originalParams, hookResult) {
  return hookResult?.params && typeof hookResult.params === "object"
    ? { ...originalParams, ...hookResult.params }
    : originalParams;
}

describe("before_tool_call params ownership policy", () => {
  it("no other plugin hook returns a params key after the origin-stamping hook", () => {
    const pluginsDir = new URL("../", import.meta.url);
    const offenders = [];
    for (const entry of readdirSync(pluginsDir, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name === "nbhd-cron-enforcement") continue;
      const indexUrl = new URL(`${entry.name}/index.js`, pluginsDir);
      let source;
      try {
        source = readFileSync(indexUrl, "utf8");
      } catch (error) {
        if (error && error.code === "ENOENT") continue;
        throw error;
      }
      if (!/api\.on\(["']before_tool_call["']/.test(source)) continue;
      if (/return\s*\{\s*params(?:\s*[:,}])/.test(source)) offenders.push(entry.name);
    }

    // Source-text guard only: it deliberately does not parse JavaScript or
    // detect aliases/computed keys. It catches the direct `{ params }` return
    // shape whose last-defined-wins behavior is pinned at
    // hook-runner-global-BdHeqZIb.js:722.
    assert.deepEqual(offenders, []);
  });
});

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

describe("register() wires up the jobId/runId seam", () => {
  it("declares hook activation in the manifest so the pinned lazy loader imports it", () => {
    const manifest = JSON.parse(readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
    assert.deepEqual(manifest.activation?.onCapabilities, ["hook"]);
  });

  it("registers all four hooks and emits a boot-visible warning", () => {
    const api = makeFakeApi();
    let registrationLog = "";
    api.logger.warn = (message) => { registrationLog = message; };
    register(api);
    assert.equal(typeof api._handlers["cron_changed"], "function");
    assert.equal(typeof api._handlers["before_prompt_build"], "function");
    assert.equal(typeof api._handlers["subagent_spawned"], "function");
    assert.equal(typeof api._handlers["before_tool_call"], "function");
    assert.match(registrationLog, /registered .*subagent_spawned/);
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
      runId: "some-chat-run",
      toolName: "nbhd_send_to_user",
      params: { message: "anything" },
    });
    assert.equal(result, undefined);
  });

  it("a cron with no contract (freeform/legacy) caches nothing — before_tool_call no-ops", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-freeform", { name: "freeform-job" }); // no description
    const result = beforeToolCall({
      runId: "run-freeform",
      toolName: "nbhd_send_to_user",
      params: { message: "anything goes" },
    });
    assert.equal(result, undefined);
  });

  it("end-to-end: rewrite action swaps the message, direct dispatch shape", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-1", {
        name: "hydrate",
        description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }),
    });

    const result = beforeToolCall({
      runId: "run-1",
      toolName: "nbhd_send_to_user",
      params: { message: "You should stay hydrated", job_name: "hydrate" },
    });
    assert.deepEqual(result, { params: { message: "Drink water", job_name: "hydrate" } });
  });

  it("end-to-end: rewrite action swaps the message, toolSearch meta dispatch shape (params.params)", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-2", {
        name: "hydrate",
        description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }),
    });

    const result = beforeToolCall({
      runId: "run-2",
      toolName: "tool_call",
      params: { id: "nbhd_send_to_user", params: { message: "wrong text", job_name: "hydrate" } },
    });
    assert.deepEqual(result, { params: { id: "nbhd_send_to_user", params: { message: "Drink water", job_name: "hydrate" } } });
  });

  it("end-to-end: rewrite action swaps the message, toolSearch meta dispatch shape (params.arguments)", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-3", { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) });

    const result = beforeToolCall({
      runId: "run-3",
      toolName: "tool_call",
      params: { id: "nbhd_send_to_user", arguments: { message: "wrong text" } },
    });
    assert.deepEqual(result, { params: { id: "nbhd_send_to_user", arguments: { message: "Drink water" } } });
  });

  it("end-to-end: revise_then_rewrite blocks under budget, then rewrites once exhausted", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-4", {
        name: "appt",
        description: contract(
          { kind: "contains", text: "appointment Tuesday 3pm" },
          { action: "revise_then_rewrite", content: "appointment Tuesday 3pm", max_revisions: 1 },
          "quote_user_intent",
        ),
    });

    const badCall = { runId: "run-4", toolName: "nbhd_send_to_user", params: { message: "no verbatim text" } };
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
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-5", {
        name: "weekly-tasks",
        description: contract(
          { kind: "marker", marker: "[block: task_summary]" },
          { action: "revise_then_allow", max_revisions: 1 },
          "domain_summary",
        ),
    });

    const badCall = { runId: "run-5", toolName: "nbhd_send_to_user", params: { message: "no marker here" } };
    assert.equal(beforeToolCall(badCall).block, true);
    // Budget exhausted — ships as-is (undefined = no interference with the call).
    assert.equal(beforeToolCall(badCall), undefined);
  });

  it("a passing message is never touched", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-6", { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) });

    const result = beforeToolCall({
      runId: "run-6",
      toolName: "nbhd_send_to_user",
      params: { message: "Drink water" },
    });
    assert.equal(result, undefined);
  });

  it("finished/removed clears the jobId-keyed contract cache", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-7", { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) }, "job-7");
    cronChanged({ action: "finished", jobId: "job-7" });

    assert.equal(
      beforeToolCall({ runId: "run-7", toolName: "nbhd_send_to_user", params: { message: "bad" } }),
      undefined,
    );
  });

  it("joins started jobId to before_prompt_build runId", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    startCron(api, "run-8", { name: "hydrate", description: contract({ kind: "contains", text: "Drink water" }, { action: "rewrite", content: "Drink water" }) });

    const result = beforeToolCall({ runId: "run-8", toolName: "nbhd_send_to_user", params: { message: "wrong" } });
    assert.deepEqual(result, { params: { message: "Drink water" } });
  });

  it("recovers the pinned runtime's omitted jobId from its isolated cron session key", () => {
    const api = makeFakeApi({ tenantId: "tenant-pinned-context", internalApiKey: "key-pinned-context" });
    register(api);
    api._handlers["before_prompt_build"]({}, {
      trigger: "cron",
      runId: "run-pinned-context",
      sessionKey: "agent:main:cron:job-pinned-context:run:run-pinned-context",
    });
    const result = api._handlers["before_tool_call"]({
      runId: "run-pinned-context",
      toolName: "nbhd_datebook_add_apple_reminder",
      params: { items: [] },
    });
    assert.equal(result.params._nbhd_origin.run_id, "run-pinned-context");
    assert.equal(result.params._nbhd_origin.job_id, "job-pinned-context");
  });

  it("does not trust a cron session key whose run segment disagrees with ctx.runId", () => {
    const api = makeFakeApi({ tenantId: "tenant-pinned-context", internalApiKey: "key-pinned-context" });
    register(api);
    api._handlers["before_prompt_build"]({}, {
      trigger: "cron",
      runId: "run-actual",
      sessionKey: "agent:main:cron:job-forged:run:run-other",
    });
    const result = api._handlers["before_tool_call"]({
      runId: "run-actual",
      toolName: "nbhd_datebook_add_apple_reminder",
      params: { items: [] },
    });
    assert.equal(result.params._nbhd_origin, null);
  });

  it("joins a spawned helper runId to its requester cron run", () => {
    const api = makeFakeApi({ tenantId: "tenant-helper-origin", internalApiKey: "key-helper-origin" });
    register(api);
    api._handlers["before_prompt_build"]({}, {
      trigger: "cron",
      jobId: "job-helper-origin",
      runId: "run-helper-parent",
      sessionKey: "agent:main:cron:job-helper-origin:run:run-helper-parent",
    });
    api._handlers["subagent_spawned"]({
      runId: "run-helper-child",
      childSessionKey: "agent:main:subagent:child",
      agentId: "main",
      mode: "run",
      threadRequested: false,
    }, {
      runId: "run-helper-child",
      childSessionKey: "agent:main:subagent:child",
      requesterSessionKey: "agent:main:cron:job-helper-origin:run:run-helper-parent",
    });

    const result = api._handlers["before_tool_call"]({
      runId: "run-helper-child",
      toolName: "nbhd_datebook_add_apple_reminder",
      params: { items: [], _nbhd_origin: { sig: "forged" } },
    });
    assert.equal(result.params._nbhd_origin.kind, "cron");
    assert.equal(result.params._nbhd_origin.run_id, "run-helper-child");
    assert.equal(result.params._nbhd_origin.job_id, "job-helper-origin");
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

      startCron(api, "run-old", { name: "old", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) });
      assert.equal(
        beforeToolCall({ runId: "run-old", toolName: "nbhd_send_to_user", params: { message: "bad" } })?.params
          ?.message,
        "hi",
      );

      now += 61_000; // past the 60s ttl
      startCron(api, "run-new", { name: "new", description: contract({ kind: "contains", text: "yo" }, { action: "rewrite", content: "yo" }) });

      // job-run-old's entry should have been pruned by the next cron_changed;
      // the top of this second cron_changed — before_tool_call now sees a miss.
      assert.equal(
        beforeToolCall({ runId: "run-old", toolName: "nbhd_send_to_user", params: { message: "bad" } }),
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

describe("signed cron origin", () => {
  const tenantId = "00000000-0000-4000-8000-000000000123";
  const internalKey = "origin-test-key";
  const makeOriginApi = () => makeFakeApi({ tenantId, internalApiKey: internalKey });

  it("adds the exact HMAC-SHA256 stamp and overwrites a flat caller stamp", () => {
    const api = makeOriginApi();
    register(api);
    const realNow = Date.now;
    try {
      Date.now = () => 1_800_000_123_456;
      startCron(api, "run-origin-flat", { name: "Morning Briefing" }, "job-origin-flat");
      const originalParams = { name: "Friday plan", _nbhd_origin: { sig: "forged" } };
      const result = api._handlers["before_tool_call"]({
        runId: "run-origin-flat",
        toolName: "nbhd_cron_create_pure_reminder",
        params: originalParams,
      });
      const ts = 1_800_000_123;
      const message = `nbhd-origin.v1|${tenantId}|cron|run-origin-flat|job-origin-flat|${ts}`;
      const sig = createHmac("sha256", internalKey).update(message).digest("hex");
      assert.deepEqual(mergeLikePinnedSdk(originalParams, result), {
          name: "Friday plan",
          _nbhd_origin: {
            v: 1,
            kind: "cron",
            tenant_id: tenantId,
            run_id: "run-origin-flat",
            job_id: "job-origin-flat",
            ts,
            sig,
          },
      });
    } finally {
      Date.now = realNow;
    }
  });

  it("strips caller origin for unknown runs in flat and both meta envelopes", () => {
    const api = makeOriginApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];
    const flat = { items: [], _nbhd_origin: { sig: "forged" } };
    const flatResult = beforeToolCall({ runId: "unknown-flat", toolName: "nbhd_datebook_add_event", params: flat });
    assert.equal(mergeLikePinnedSdk(flat, flatResult)._nbhd_origin, null);

    const metaParams = { id: "nbhd_cron_create_domain_summary", params: { name: "x", _nbhd_origin: { sig: "forged" } } };
    const metaParamsResult = beforeToolCall({ runId: "unknown-meta-params", toolName: "tool_call", params: metaParams });
    assert.equal(mergeLikePinnedSdk(metaParams, metaParamsResult).params._nbhd_origin, null);

    const metaArguments = { id: "nbhd_datebook_add_apple_reminder", arguments: { items: [], _nbhd_origin: { sig: "forged" } } };
    const metaArgumentsResult = beforeToolCall({ runId: "unknown-meta-arguments", toolName: "tool_call", params: metaArguments });
    assert.equal(mergeLikePinnedSdk(metaArguments, metaArgumentsResult).arguments._nbhd_origin, null);
  });

  it("overwrites caller origin inside a meta envelope and preserves it", () => {
    const api = makeOriginApi();
    register(api);
    startCron(api, "run-origin-meta", { name: "Week Ahead" }, "job-origin-meta");
    const originalParams = { id: "nbhd_datebook_add_event", trace: "preserved", arguments: { items: [{ title: "Plan" }], _nbhd_origin: { sig: "forged" } } };
    const result = api._handlers["before_tool_call"]({
      runId: "run-origin-meta",
      toolName: "tool_call",
      params: originalParams,
    });
    const merged = mergeLikePinnedSdk(originalParams, result);
    assert.equal(merged.id, "nbhd_datebook_add_event");
    assert.equal(merged.trace, "preserved");
    assert.deepEqual(merged.arguments.items, [{ title: "Plan" }]);
    assert.equal(merged.arguments._nbhd_origin.kind, "cron");
    assert.equal(merged.arguments._nbhd_origin.run_id, "run-origin-meta");
    assert.equal(merged.arguments._nbhd_origin.job_id, "job-origin-meta");
    assert.notEqual(merged.arguments._nbhd_origin.sig, "forged");
  });

  it("returns stripped params even when enforcement throws afterward", () => {
    const api = makeOriginApi();
    register(api);
    startCron(api, "run-origin-error", { name: "guarded", description: contract({ kind: "contains", text: "x" }, { action: "rewrite", content: "x" }) });
    const originalParams = { name: "x", _nbhd_origin: { sig: "forged" } };
    const event = { toolName: "nbhd_cron_create_pure_reminder", params: originalParams };
    Object.defineProperty(event, "runId", {
      enumerable: true,
      get() {
        throw new Error("enforcement lookup failed");
      },
    });
    let result;
    assert.doesNotThrow(() => {
      result = api._handlers["before_tool_call"](event);
    });
    const merged = mergeLikePinnedSdk(originalParams, result);
    assert.equal(merged.name, "x");
    assert.equal(merged._nbhd_origin, null);
  });

  it("before_prompt_build ignores non-cron and incomplete contexts", () => {
    const api = makeOriginApi();
    register(api);
    const beforePromptBuild = api._handlers["before_prompt_build"];
    assert.equal(beforePromptBuild({}, { trigger: "user", runId: "run-user", jobId: "job-user" }), undefined);
    assert.equal(beforePromptBuild({}, { trigger: "cron", runId: "run-no-job" }), undefined);
    const result = api._handlers["before_tool_call"]({ runId: "run-user", toolName: "nbhd_cron_create_pure_reminder", params: { name: "x" } });
    assert.equal(result.params._nbhd_origin, null);
  });
});

describe("before_tool_call throw-safety", () => {
  it("a structurally poisoned contract (check=null, wrong-typed leaves) never throws", () => {
    const api = makeFakeApi();
    register(api);
    const cronChanged = api._handlers["cron_changed"];
    const beforeToolCall = api._handlers["before_tool_call"];

    const poisoned =
      PREFIX + JSON.stringify({ v: 1, pattern: "x", check: null, on_fail: { action: "rewrite", content: 12345 } });
    startCron(api, "poison-1", { name: "p", description: poisoned });

    assert.doesNotThrow(() =>
      beforeToolCall({ runId: "poison-1", toolName: "nbhd_send_to_user", params: { message: "hi" } }),
    );
  });

  it("a contract with a garbage on_fail.action never throws", () => {
    const api = makeFakeApi();
    register(api);
    const beforeToolCall = api._handlers["before_tool_call"];

    const poisoned = PREFIX + JSON.stringify({ v: 1, pattern: "x", check: { kind: "bogus" }, on_fail: 42 });
    startCron(api, "poison-2", { name: "p2", description: poisoned });

    assert.doesNotThrow(() =>
      beforeToolCall({ runId: "poison-2", toolName: "nbhd_send_to_user", params: { message: "hi" } }),
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
    Object.defineProperty(poisonedEvent, "runId", {
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
    Object.defineProperty(poisonedEvent, "jobId", {
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

    startCron(api, "poison-3", { name: "p3", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) });

    for (const garbage of [
      null,
      undefined,
      {},
      { toolName: 42 },
      { toolName: "tool_call", params: null },
      { runId: "poison-3", toolName: "nbhd_send_to_user", params: null },
      { runId: "poison-3", toolName: "tool_call", params: { id: "nbhd_send_to_user", params: "not an object" } },
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
      cronChanged({ action: "started", jobId: "job-s-log-1", job: { name: "j", description: "nbhd.v1 {not json" } }),
    );

    startCron(throwingApi, "s-log-2", { name: "j2", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) });
    assert.doesNotThrow(() =>
      beforeToolCall({ runId: "s-log-2", toolName: "nbhd_send_to_user", params: { message: "hi" } }),
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

    startCron(throwingApi, "s-log-3", { name: "j3", description: contract({ kind: "contains", text: "hi" }, { action: "rewrite", content: "hi" }) });

    let result;
    assert.doesNotThrow(() => {
      result = beforeToolCall({ runId: "s-log-3", toolName: "nbhd_send_to_user", params: { message: "wrong text" } });
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
      cronChanged({ action: "started", jobId: "job-s-log-4", job: { name: "j4", description: undefined } }),
    );
  });
});

// ── fire-time hard caps (limits) ─────────────────────────────────────────────
// The JS twin of apps/cron/tests/test_patterns.py::TaskHygieneLimitsTests and
// test_task_hygiene_seeding.py's baked-contract assertions. Python proves the
// contract is BAKED with limits; these prove the limits are ENFORCED. Both
// sides must agree on the shape {sends, mutations} — update both tables.

describe("decideLimitAction (unit)", () => {
  const withLimits = { limits: { sends: 1, mutations: 10 } };

  it("counts a send when the budget is unspent", () => {
    assert.deepEqual(decideLimitAction(withLimits, "nbhd_send_to_user", { sends: 0 }), {
      type: "count",
      counter: "sends",
    });
  });

  it("blocks a send once the budget is spent", () => {
    const decision = decideLimitAction(withLimits, "nbhd_send_to_user", { sends: 1 });
    assert.equal(decision.type, "block");
    assert.match(decision.reason, /already sent/);
  });

  it("blocks a mutation only once the budget is spent", () => {
    assert.equal(decideLimitAction(withLimits, "nbhd_task_skip", { mutations: 9 }).type, "count");
    const decision = decideLimitAction(withLimits, "nbhd_task_skip", { mutations: 10 });
    assert.equal(decision.type, "block");
    assert.match(decision.reason, /PROPOSALS/);
  });

  it("all three lifecycle tools count against the same mutation budget", () => {
    for (const tool of ["nbhd_task_complete", "nbhd_task_skip", "nbhd_task_defer"]) {
      assert.equal(decideLimitAction(withLimits, tool, { mutations: 10 }).type, "block", tool);
    }
  });

  it("a contract with NO limits key is uncapped (briefings et al. unaffected)", () => {
    const noLimits = { check: { kind: "marker", marker: "[block: daily_briefing]" } };
    assert.deepEqual(decideLimitAction(noLimits, "nbhd_send_to_user", { sends: 99 }), { type: "pass" });
    assert.deepEqual(decideLimitAction(noLimits, "nbhd_task_skip", { mutations: 99 }), { type: "pass" });
  });

  it("garbage limits fail open rather than blocking", () => {
    for (const limits of [null, "nope", [], { sends: "one" }, { sends: NaN }]) {
      assert.equal(decideLimitAction({ limits }, "nbhd_send_to_user", { sends: 5 }).type, "pass");
    }
  });

  it("a read-only query tool never counts as a mutation", () => {
    assert.deepEqual(decideLimitAction(withLimits, "nbhd_task_list", { mutations: 10 }), { type: "pass" });
  });
});

describe("resolveDispatchedToolId", () => {
  it("resolves a direct dispatch", () => {
    assert.equal(resolveDispatchedToolId({ toolName: "nbhd_task_complete" }), "nbhd_task_complete");
  });

  it("resolves through the toolSearch meta dispatch", () => {
    assert.equal(
      resolveDispatchedToolId({ toolName: "tool_call", params: { id: "nbhd_task_skip" } }),
      "nbhd_task_skip",
    );
  });
});

describe("end-to-end caps via register() — the real before_tool_call path", () => {
  it("the FIRST summary goes out and the SECOND is blocked outright", () => {
    const beforeToolCall = startHygieneCron("cap-sends");

    const first = beforeToolCall(sendCall("cap-sends", `${HYGIENE_MARKER}\nClosed 2, deferred 1.`));
    assert.equal(first, undefined, "first send must pass through untouched");

    const second = beforeToolCall(sendCall("cap-sends", `${HYGIENE_MARKER}\nAlso, one more thing.`));
    assert.equal(second.block, true);
    assert.match(second.blockReason, /already sent/);
  });

  it("two interleaved runs of the same job have independent send budgets", () => {
    const api = makeFakeApi();
    register(api);
    const jobId = "job-overlap";
    const job = { name: "Task Hygiene", description: hygieneContract() };
    startCron(api, "run-overlap-a", job, jobId);
    assert.equal(api._handlers["before_prompt_build"]({}, {
      trigger: "cron",
      jobId,
      runId: "run-overlap-b",
    }), undefined);

    const beforeToolCall = api._handlers["before_tool_call"];
    assert.equal(beforeToolCall(sendCall("run-overlap-a", `${HYGIENE_MARKER}\nrun A first`)), undefined);
    assert.equal(beforeToolCall(sendCall("run-overlap-b", `${HYGIENE_MARKER}\nrun B first`)), undefined);
    assert.equal(beforeToolCall(sendCall("run-overlap-a", `${HYGIENE_MARKER}\nrun A second`)).block, true);
    assert.equal(beforeToolCall(sendCall("run-overlap-b", `${HYGIENE_MARKER}\nrun B second`)).block, true);
  });

  it("a repeated started event refreshes the job contract without resetting a live run", () => {
    const api = makeFakeApi();
    register(api);
    const jobId = "job-reemitted-start";
    const job = { name: "Task Hygiene", description: hygieneContract() };
    startCron(api, "run-reemitted-start", job, jobId);
    const beforeToolCall = api._handlers["before_tool_call"];
    assert.equal(
      beforeToolCall(sendCall("run-reemitted-start", `${HYGIENE_MARKER}\nfirst`)),
      undefined,
    );

    api._handlers["cron_changed"]({
      jobId,
      action: "started",
      job,
      runAtMs: Date.now(),
    });
    const second = beforeToolCall(sendCall("run-reemitted-start", `${HYGIENE_MARKER}\nsecond`));
    assert.equal(second.block, true);
    assert.match(second.blockReason, /already sent/);
  });

  it("a repeated before_prompt_build for one run preserves its counters", () => {
    const api = makeFakeApi();
    register(api);
    const jobId = "job-double-prompt";
    startCron(api, "run-double-prompt", {
      name: "Task Hygiene",
      description: hygieneContract(),
    }, jobId);
    const beforeToolCall = api._handlers["before_tool_call"];
    assert.equal(beforeToolCall(sendCall("run-double-prompt", `${HYGIENE_MARKER}\nfirst`)), undefined);

    assert.equal(api._handlers["before_prompt_build"]({}, {
      trigger: "cron",
      jobId,
      runId: "run-double-prompt",
    }), undefined);
    const second = beforeToolCall(sendCall("run-double-prompt", `${HYGIENE_MARKER}\nsecond`));
    assert.equal(second.block, true);
    assert.match(second.blockReason, /already sent/);
  });

  it("a second send is refused even when its content is perfectly valid", () => {
    // The cap is structural, not a content judgement — this is the ONE-sender
    // guarantee, and it holds regardless of how good message two looks.
    const beforeToolCall = startHygieneCron("cap-sends-valid");
    beforeToolCall(sendCall("cap-sends-valid", `${HYGIENE_MARKER}\nfirst`));
    assert.equal(beforeToolCall(sendCall("cap-sends-valid", `${HYGIENE_MARKER}\nsecond`)).block, true);
  });

  it("a blocked revise does NOT burn the send budget", () => {
    // Regression guard: if the revise-block counted as a send, asking the model
    // to add its missing marker would cost it the only message it was allowed
    // to send, and the hygiene summary would never arrive.
    const beforeToolCall = startHygieneCron("cap-revise");

    const missingMarker = beforeToolCall(sendCall("cap-revise", "I tidied up your tasks."));
    assert.equal(missingMarker.block, true);
    assert.match(missingMarker.blockReason, /marker/);

    const corrected = beforeToolCall(sendCall("cap-revise", `${HYGIENE_MARKER}\nClosed 2.`));
    assert.equal(corrected, undefined, "the corrected first send must still be allowed");

    const extra = beforeToolCall(sendCall("cap-revise", `${HYGIENE_MARKER}\nOne more.`));
    assert.equal(extra.block, true, "but the budget is spent now");
  });

  it("ten task changes pass and the eleventh is blocked", () => {
    const beforeToolCall = startHygieneCron("cap-mutations");

    for (let i = 0; i < 10; i += 1) {
      assert.equal(beforeToolCall(mutationCall("cap-mutations")), undefined, `mutation ${i + 1} must pass`);
    }

    const eleventh = beforeToolCall(mutationCall("cap-mutations"));
    assert.equal(eleventh.block, true);
    assert.match(eleventh.blockReason, /PROPOSALS/);
  });

  it("the mutation budget is shared across complete/skip/defer", () => {
    const beforeToolCall = startHygieneCron("cap-mixed", { sends: 1, mutations: 3 });
    assert.equal(beforeToolCall(mutationCall("cap-mixed", "nbhd_task_complete")), undefined);
    assert.equal(beforeToolCall(mutationCall("cap-mixed", "nbhd_task_skip")), undefined);
    assert.equal(beforeToolCall(mutationCall("cap-mixed", "nbhd_task_defer")), undefined);
    assert.equal(beforeToolCall(mutationCall("cap-mixed", "nbhd_task_complete")).block, true);
  });

  it("mutations are capped through the toolSearch meta dispatch too", () => {
    const beforeToolCall = startHygieneCron("cap-meta", { sends: 1, mutations: 1 });
    assert.equal(
      beforeToolCall({ runId: "cap-meta", toolName: "tool_call", params: { id: "nbhd_task_skip" } }),
      undefined,
    );
    const blocked = beforeToolCall({
      runId: "cap-meta",
      toolName: "tool_call",
      params: { id: "nbhd_task_skip" },
    });
    assert.equal(blocked.block, true);
  });

  it("read-only query tools are never capped", () => {
    const beforeToolCall = startHygieneCron("cap-reads", { sends: 1, mutations: 1 });
    for (let i = 0; i < 20; i += 1) {
      assert.equal(beforeToolCall({ runId: "cap-reads", toolName: "nbhd_task_list", params: {} }), undefined);
    }
    // The mutation budget is untouched by all that reading.
    assert.equal(beforeToolCall(mutationCall("cap-reads")), undefined);
  });

  it("a daily_briefing contract carries no limits and stays uncapped", () => {
    // The whole point of making `limits` optional: existing patterns must not
    // acquire a cap by accident.
    const api = makeFakeApi();
    register(api);
    startCron(api, "briefing", {
        name: "Morning Briefing",
        description: contract(
          { kind: "marker", marker: "[block: daily_briefing]" },
          { action: "revise_then_allow", max_revisions: 1 },
          "daily_briefing",
        ),
    });
    const beforeToolCall = api._handlers["before_tool_call"];

    for (let i = 0; i < 5; i += 1) {
      const result = beforeToolCall({
        runId: "briefing",
        toolName: "nbhd_send_to_user",
        params: { message: "[block: daily_briefing]\nGood morning!" },
      });
      assert.equal(result, undefined, `briefing send ${i + 1} must not be capped`);
    }
  });

  it("each cron session gets its own budget", () => {
    const first = startHygieneCron("cap-session-a");
    const second = startHygieneCron("cap-session-b");
    first(sendCall("cap-session-a", `${HYGIENE_MARKER}\na`));
    assert.equal(first(sendCall("cap-session-a", `${HYGIENE_MARKER}\na2`)).block, true);
    assert.equal(second(sendCall("cap-session-b", `${HYGIENE_MARKER}\nb`)), undefined);
  });
});
