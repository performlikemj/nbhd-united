import assert from "node:assert/strict";
import { test } from "node:test";

import register, { phaseForEvent, realToolId, postProgress } from "./index.js";

test("realToolId unwraps tool_call dispatch to the real id", () => {
  assert.equal(realToolId({ toolName: "tool_call", params: { id: "nbhd_journal_add" } }), "nbhd_journal_add");
  assert.equal(realToolId({ toolName: "tool_call", params: { name: "nbhd_finance_summary" } }), "nbhd_finance_summary");
  assert.equal(realToolId({ toolName: "nbhd_fuel_log_workout" }), "nbhd_fuel_log_workout");
  assert.equal(realToolId({}), "");
});

test("phaseForEvent maps tool families to friendly phrases", () => {
  assert.deepEqual(phaseForEvent({ toolName: "tool_call", params: { id: "nbhd_journal_search" } }), {
    phase: "tool",
    detail: "checking your journal",
  });
  assert.deepEqual(phaseForEvent({ toolName: "tool_call", params: { id: "nbhd_finance_record_payment" } }), {
    phase: "tool",
    detail: "checking your finances",
  });
  assert.deepEqual(phaseForEvent({ toolName: "nbhd_fuel_summary" }), {
    phase: "tool",
    detail: "looking at your fitness",
  });
  assert.deepEqual(phaseForEvent({ toolName: "nbhd_task_list" }), {
    phase: "tool",
    detail: "checking your tasks and goals",
  });
});

test("phaseForEvent treats catalog meta-tools as thinking", () => {
  assert.deepEqual(phaseForEvent({ toolName: "tool_search" }), { phase: "thinking", detail: "" });
  assert.deepEqual(phaseForEvent({ toolName: "tool_describe" }), { phase: "thinking", detail: "" });
});

test("phaseForEvent maps exact site and portfolio tools before family phrases", () => {
  const expected = {
    site_list_files: "reviewing site files",
    site_read_file: "reading your site",
    site_stage_file: "preparing site changes",
    site_stage_upload: "preparing a site image",
    site_show_pending: "reviewing site changes",
    site_discard: "discarding site changes",
    site_publish: "publishing your site",
    site_deploy_status: "checking your site deployment",
    publish_portfolio_image: "publishing a portfolio image",
  };
  for (const [toolName, detail] of Object.entries(expected)) {
    assert.deepEqual(phaseForEvent({ toolName }), { phase: "tool", detail });
  }
});

test("phaseForEvent derives a safe phrase for unknown tools", () => {
  assert.deepEqual(phaseForEvent({ toolName: "nbhd_foo_bar_2" }), { phase: "tool", detail: "foo bar" });
  assert.deepEqual(phaseForEvent({ toolName: "x" }), { phase: "tool", detail: "working on it" });
});

test("phaseForEvent unwraps a meta-dispatched site tool before labeling it", () => {
  assert.deepEqual(phaseForEvent({ toolName: "tool_call", params: { id: "site_publish" } }), {
    phase: "tool",
    detail: "publishing your site",
  });
});

test("phaseForEvent returns null when there is no tool id", () => {
  assert.equal(phaseForEvent({}), null);
  assert.equal(phaseForEvent({ toolName: "tool_call", params: {} }), null);
});

test("postProgress is a no-op (never throws) when runtime env is unset", async () => {
  const prev = {
    base: process.env.NBHD_API_BASE_URL,
    tenant: process.env.NBHD_TENANT_ID,
    key: process.env.NBHD_INTERNAL_API_KEY,
  };
  delete process.env.NBHD_API_BASE_URL;
  delete process.env.NBHD_TENANT_ID;
  delete process.env.NBHD_INTERNAL_API_KEY;
  try {
    // Must resolve without throwing and without attempting a fetch.
    await postProgress("tool", "checking your journal", { logger: { debug() {} } });
  } finally {
    if (prev.base !== undefined) process.env.NBHD_API_BASE_URL = prev.base;
    if (prev.tenant !== undefined) process.env.NBHD_TENANT_ID = prev.tenant;
    if (prev.key !== undefined) process.env.NBHD_INTERNAL_API_KEY = prev.key;
  }
});

async function withProgressRuntime(fetchImpl, run) {
  const previous = {
    base: process.env.NBHD_API_BASE_URL,
    tenant: process.env.NBHD_TENANT_ID,
    key: process.env.NBHD_INTERNAL_API_KEY,
    fetch: globalThis.fetch,
  };
  process.env.NBHD_API_BASE_URL = "https://api.example.test";
  process.env.NBHD_TENANT_ID = "tenant-1";
  process.env.NBHD_INTERNAL_API_KEY = "test-key";
  globalThis.fetch = fetchImpl;
  try {
    await run();
  } finally {
    if (previous.base === undefined) delete process.env.NBHD_API_BASE_URL;
    else process.env.NBHD_API_BASE_URL = previous.base;
    if (previous.tenant === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = previous.tenant;
    if (previous.key === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = previous.key;
    globalThis.fetch = previous.fetch;
  }
}

test("postProgress warns for a non-ok response", async () => {
  const warnings = [];
  await withProgressRuntime(
    async () => ({ ok: false, status: 500 }),
    async () => {
      await postProgress("tool", "publishing your site", {
        logger: { warn(message) { warnings.push(message); }, debug() {} },
      });
    },
  );
  assert.deepEqual(warnings, ["nbhd-activity-stream: progress post http 500"]);
});

test("postProgress warns when an ok response is not attributed", async () => {
  const warnings = [];
  await withProgressRuntime(
    async () => ({ ok: true, status: 200, async json() { return { updated: false }; } }),
    async () => {
      await postProgress("tool", "publishing your site", {
        logger: { warn(message) { warnings.push(message); }, debug() {} },
      });
    },
  );
  assert.deepEqual(warnings, ["nbhd-activity-stream: progress not attributed (updated=false)"]);
});

test("postProgress does not warn when an ok response is attributed", async () => {
  const warnings = [];
  await withProgressRuntime(
    async () => ({ ok: true, status: 200, async json() { return { updated: true }; } }),
    async () => {
      await postProgress("tool", "publishing your site", {
        logger: { warn(message) { warnings.push(message); }, debug() {} },
      });
    },
  );
  assert.deepEqual(warnings, []);
});

test("register logs one argument-free debug line for each emission", () => {
  const hooks = {};
  const debugLines = [];
  register({
    on(name, callback) { hooks[name] = callback; },
    logger: { warn() {}, debug(message) { debugLines.push(message); } },
  });

  hooks.before_tool_call({
    toolName: "tool_call",
    params: { id: "site_publish", secretArgument: "must-not-appear" },
  });
  hooks.before_agent_finalize();

  assert.deepEqual(debugLines, [
    "nbhd-activity-stream: emit tool=site_publish phase=tool detail=publishing-your-site",
    "nbhd-activity-stream: emit tool=none phase=composing detail=",
  ]);
  assert.equal(debugLines.join("\n").includes("must-not-appear"), false);
});
