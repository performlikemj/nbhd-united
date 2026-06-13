"use strict";

/**
 * Tests for tool-logger.js — exercised via `node --test`.
 *
 * Run from repo root:
 *   node --test runtime/openclaw/tool-logger.test.js
 *
 * These pin:
 *   - call/ok lines fire on success, in order, with matching id + duration
 *   - error line fires on throw, original error rethrows (no swallow)
 *   - call args/return value are NOT logged (privacy)
 *   - registration line shape
 *   - wrapTool validates its input
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { wrapTool, logToolRegistered, __internals } = require("./tool-logger");

function captureConsole() {
  const captured = [];
  const original = console.log;
  console.log = (...args) => {
    captured.push(args.join(" "));
  };
  return {
    captured,
    restore: () => {
      console.log = original;
    },
  };
}

test("wrapTool emits call + ok on success", async () => {
  const def = wrapTool({
    name: "nbhd_test_ok",
    parameters: {},
    async execute(_id, _params) {
      return "ok-result";
    },
  });
  const cap = captureConsole();
  try {
    const result = await def.execute("agent-call-1", { foo: "bar" });
    assert.equal(result, "ok-result");
    assert.equal(cap.captured.length, 2);
    assert.match(cap.captured[0], /^\[nbhd:tools\] call nbhd_test_ok id=\d+$/);
    assert.match(
      cap.captured[1],
      /^\[nbhd:tools\] ok nbhd_test_ok id=\d+ duration=\d+ms$/,
    );
    // call and ok share the same id
    const callId = cap.captured[0].match(/id=(\d+)/)[1];
    const okId = cap.captured[1].match(/id=(\d+)/)[1];
    assert.equal(callId, okId);
  } finally {
    cap.restore();
  }
});

test("wrapTool emits call + error on throw, original error rethrows", async () => {
  const sentinel = new Error("kaboom");
  const def = wrapTool({
    name: "nbhd_test_err",
    parameters: {},
    async execute() {
      throw sentinel;
    },
  });
  const cap = captureConsole();
  try {
    await assert.rejects(() => def.execute("id-x", {}), (err) => {
      assert.equal(err, sentinel);
      return true;
    });
    assert.equal(cap.captured.length, 2);
    assert.match(cap.captured[0], /^\[nbhd:tools\] call nbhd_test_err id=\d+$/);
    assert.match(
      cap.captured[1],
      /^\[nbhd:tools\] error nbhd_test_err id=\d+ duration=\d+ms message=kaboom$/,
    );
  } finally {
    cap.restore();
  }
});

test("wrapTool does NOT log args or return value (privacy)", async () => {
  const secretArg = "user_text_we_must_not_leak";
  const secretReturn = "assistant_reply_we_must_not_leak";
  const def = wrapTool({
    name: "nbhd_test_privacy",
    parameters: {},
    async execute(_id, _params) {
      return secretReturn;
    },
  });
  const cap = captureConsole();
  try {
    await def.execute("call-id", { message: secretArg });
    const all = cap.captured.join("\n");
    assert.equal(all.includes(secretArg), false, "arg leaked into logs");
    assert.equal(all.includes(secretReturn), false, "return value leaked into logs");
  } finally {
    cap.restore();
  }
});

test("wrapTool error log truncates long messages", async () => {
  const longMsg = "x".repeat(500);
  const def = wrapTool({
    name: "nbhd_test_truncate",
    parameters: {},
    async execute() {
      throw new Error(longMsg);
    },
  });
  const cap = captureConsole();
  try {
    await assert.rejects(() => def.execute("id", {}));
    const errLine = cap.captured[1];
    // The whole log line should be bounded — error message capped at 200 chars
    assert.ok(errLine.length < 350, `error line too long: ${errLine.length}`);
    assert.ok(
      errLine.includes("message=" + "x".repeat(200)),
      "expected message field capped at 200 chars",
    );
  } finally {
    cap.restore();
  }
});

test("wrapTool preserves other tool-def fields", () => {
  const original = {
    name: "nbhd_test_passthrough",
    description: "test description",
    parameters: { type: "object", properties: { foo: { type: "string" } } },
    async execute() {
      return "ok";
    },
  };
  const wrapped = wrapTool(original);
  assert.equal(wrapped.name, original.name);
  assert.equal(wrapped.description, original.description);
  assert.deepEqual(wrapped.parameters, original.parameters);
  assert.notEqual(wrapped.execute, original.execute, "execute must be wrapped");
});

test("wrapTool validates required fields", () => {
  assert.throws(() => wrapTool(null), TypeError);
  assert.throws(() => wrapTool({}), TypeError);
  assert.throws(() => wrapTool({ name: "x" }), TypeError); // missing execute
  assert.throws(() => wrapTool({ name: "", execute: async () => {} }), TypeError);
});

test("wrapTool passes all execute args through unchanged (variadic)", async () => {
  // Some legacy plugins destructure the first arg as params (no toolCallId).
  // The wrapper must not constrain to a specific arity.
  let received = null;
  const def = wrapTool({
    name: "nbhd_test_variadic",
    parameters: {},
    async execute(...args) {
      received = args;
      return "ok";
    },
  });
  await def.execute("call-id-x", { a: 1 }, "signal-placeholder", "onUpdate-fn");
  assert.deepEqual(received, ["call-id-x", { a: 1 }, "signal-placeholder", "onUpdate-fn"]);
});

test("logToolRegistered emits redactor-friendly line", () => {
  const cap = captureConsole();
  try {
    logToolRegistered("nbhd_some_tool", "nbhd-some-plugin");
    assert.equal(cap.captured.length, 1);
    assert.equal(
      cap.captured[0],
      "[nbhd:tools] registered tool=nbhd_some_tool plugin=nbhd-some-plugin",
    );
  } finally {
    cap.restore();
  }
});

test("wrapTool({plugin}) emits registration line at wrap time", () => {
  const cap = captureConsole();
  try {
    wrapTool(
      {
        name: "nbhd_test_register",
        parameters: {},
        async execute() {
          return "ok";
        },
      },
      { plugin: "nbhd-test-plugin" },
    );
    assert.equal(cap.captured.length, 1);
    assert.equal(
      cap.captured[0],
      "[nbhd:tools] registered tool=nbhd_test_register plugin=nbhd-test-plugin",
    );
  } finally {
    cap.restore();
  }
});

test("wrapTool() without plugin opt emits no registration line", () => {
  const cap = captureConsole();
  try {
    wrapTool({
      name: "nbhd_test_no_plugin",
      parameters: {},
      async execute() {
        return "ok";
      },
    });
    assert.equal(cap.captured.length, 0);
  } finally {
    cap.restore();
  }
});

test("safeErrorMessage handles odd inputs", () => {
  const { safeErrorMessage } = __internals;
  assert.equal(safeErrorMessage(null), "(unknown)");
  assert.equal(safeErrorMessage(undefined), "(unknown)");
  assert.equal(safeErrorMessage("bare string"), "bare string");
  assert.equal(safeErrorMessage(new Error("msg")), "msg");
  // Multi-line errors get whitespace-collapsed so they stay on one log line
  assert.equal(safeErrorMessage(new Error("line1\nline2\t\t")), "line1 line2");
  // Truncation
  const long = "a".repeat(500);
  assert.equal(safeErrorMessage(new Error(long)).length, 200);
});

// ── Required-parameter guard (added 2026-06-13) ──────────────────────────────
// Centralized presence-check so the model gets one complete error naming every
// omitted required field. Presence-only: empty strings still reach execute so
// no previously-accepted call is newly rejected.

test("required guard: omitted required param throws before execute", async () => {
  let executed = false;
  const def = wrapTool({
    name: "nbhd_test_req",
    parameters: { type: "object", properties: { section_slug: { type: "string" }, content: { type: "string" } }, required: ["section_slug", "content"] },
    async execute() { executed = true; return "should-not-run"; },
  });
  const cap = captureConsole();
  try {
    await assert.rejects(
      () => def.execute("id-1", { content: "hi" }), // section_slug omitted
      (err) => {
        assert.match(err.message, /missing required parameter\(s\): section_slug/);
        assert.match(err.message, /requires: section_slug, content/);
        return true;
      },
    );
    assert.equal(executed, false, "execute must NOT run when a required arg is missing");
    assert.equal(cap.captured.length, 2); // call + error
    assert.match(cap.captured[1], /^\[nbhd:tools\] error nbhd_test_req id=\d+ duration=\d+ms message=missing required parameter/);
  } finally {
    cap.restore();
  }
});

test("required guard: names ALL omitted required fields at once", async () => {
  const def = wrapTool({
    name: "nbhd_test_req_multi",
    parameters: { type: "object", properties: { a: {}, b: {}, c: {} }, required: ["a", "b", "c"] },
    async execute() { return "x"; },
  });
  const cap = captureConsole();
  try {
    await assert.rejects(
      () => def.execute("id-2", { b: "present" }),
      (err) => {
        assert.match(err.message, /missing required parameter\(s\): a, c/);
        return true;
      },
    );
  } finally {
    cap.restore();
  }
});

test("required guard: all required present → executes normally", async () => {
  const def = wrapTool({
    name: "nbhd_test_req_ok",
    parameters: { type: "object", properties: { task_id: {} }, required: ["task_id"] },
    async execute(_id, params) { return `ok:${params.task_id}`; },
  });
  const cap = captureConsole();
  try {
    const r = await def.execute("id-3", { task_id: "abc" });
    assert.equal(r, "ok:abc");
    assert.match(cap.captured[1], /^\[nbhd:tools\] ok nbhd_test_req_ok/);
  } finally {
    cap.restore();
  }
});

test("required guard is presence-only: empty string still reaches execute (no regression)", async () => {
  let seen;
  const def = wrapTool({
    name: "nbhd_test_req_empty",
    parameters: { type: "object", properties: { text: {} }, required: ["text"] },
    async execute(_id, params) { seen = params.text; return "ran"; },
  });
  const cap = captureConsole();
  try {
    const r = await def.execute("id-4", { text: "" }); // empty but PRESENT
    assert.equal(r, "ran", "empty-string required arg must still reach execute");
    assert.equal(seen, "");
  } finally {
    cap.restore();
  }
});

test("required guard: no required array → never blocks", async () => {
  const def = wrapTool({
    name: "nbhd_test_no_req",
    parameters: { type: "object", properties: { q: {} } },
    async execute() { return "ran"; },
  });
  const cap = captureConsole();
  try {
    assert.equal(await def.execute("id-5", {}), "ran");
  } finally {
    cap.restore();
  }
});

// ── Guard arg-position robustness (adversarial finding 2026-06-13) ────────────
// OpenClaw calls execute(toolCallId, params, signal, onUpdate) → params=args[1],
// but legacy tools (image-gen, finance gravity_query) destructure args[0]. The
// guard must read the real params object from either position and never grab
// the AbortSignal at args[2].

test("required guard reads params from args[1] (canonical toolCallId, params)", async () => {
  let ran = false;
  const def = wrapTool({
    name: "nbhd_test_canonical",
    parameters: { type: "object", properties: { prompt: {} }, required: ["prompt"] },
    async execute(_id, params) { ran = true; return params.prompt; },
  });
  // present → passes
  assert.equal(await def.execute("call-1", { prompt: "hi" }, {}, () => {}), "hi");
  assert.equal(ran, true);
  // omitted → guard throws (does not misread the toolCallId string)
  await assert.rejects(() => def.execute("call-2", {}, {}, () => {}), /missing required parameter\(s\): prompt/);
});

test("required guard reads params from args[0] for legacy single-arg execute(params)", async () => {
  const def = wrapTool({
    name: "nbhd_test_legacy_arg",
    parameters: { type: "object", properties: { prompt: {} }, required: ["prompt"] },
    async execute(params) { return params.prompt; }, // legacy: params at args[0]
  });
  // Called single-arg → params at args[0]; guard must find it, not throw.
  assert.equal(await def.execute({ prompt: "legacy" }), "legacy");
});

test("required guard never mistakes args[2] (signal) for params", async () => {
  const def = wrapTool({
    name: "nbhd_test_signal",
    parameters: { type: "object", properties: { prompt: {} }, required: ["prompt"] },
    async execute(_id, params) { return params && params.prompt; },
  });
  // params at args[1] is {} (model sent nothing); a non-null signal sits at args[2].
  // Guard must validate args[1]={} (→ prompt missing), NOT the signal object.
  await assert.rejects(
    () => def.execute("call-3", {}, { aborted: false }, () => {}),
    /missing required parameter\(s\): prompt/,
  );
});
