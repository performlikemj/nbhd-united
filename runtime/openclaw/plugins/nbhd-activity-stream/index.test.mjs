import assert from "node:assert/strict";
import { test } from "node:test";

import { phaseForEvent, realToolId, postProgress } from "./index.js";

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

test("phaseForEvent falls back to a generic phrase for unknown tools", () => {
  assert.deepEqual(phaseForEvent({ toolName: "some_unknown_tool" }), { phase: "tool", detail: "working on it" });
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
