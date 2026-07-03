import assert from "node:assert/strict";
import { test } from "node:test";

import { joinAssistantTexts, accumulate, capText, nextSeq, postPartial } from "./index.js";

test("joinAssistantTexts concatenates string pieces and drops non-strings/empties", () => {
  assert.equal(joinAssistantTexts(["Hel", "lo"]), "Hello");
  assert.equal(joinAssistantTexts(["a", "", "b", null, "c", 3]), "abc");
  assert.equal(joinAssistantTexts([]), "");
  assert.equal(joinAssistantTexts("nope"), "");
  assert.equal(joinAssistantTexts(undefined), "");
});

test("accumulate appends each step's text onto the prior cumulative", () => {
  const s1 = accumulate("", ["Thinking"]);
  assert.equal(s1, "Thinking");
  const s2 = accumulate(s1, [" about", " it"]);
  assert.equal(s2, "Thinking about it");
});

test("accumulate leaves the accumulator unchanged for an empty step", () => {
  assert.equal(accumulate("so far", []), "so far");
  assert.equal(accumulate("so far", [""]), "so far");
  assert.equal(accumulate("so far", undefined), "so far");
});

test("accumulate caps cumulative text at the 32k limit", () => {
  const big = "x".repeat(40000);
  const out = accumulate("", [big]);
  assert.equal(out.length, 32000);
  // Appending more once capped stays capped.
  const out2 = accumulate(out, ["more"]);
  assert.equal(out2.length, 32000);
});

test("capText clamps a final message to the 32k limit", () => {
  assert.equal(capText("short"), "short");
  assert.equal(capText("y".repeat(50000)).length, 32000);
  assert.equal(capText(undefined), "");
  assert.equal(capText(123), "");
});

test("nextSeq is strictly monotonic", () => {
  const a = nextSeq();
  const b = nextSeq();
  const c = nextSeq();
  assert.ok(b > a, "seq must increase");
  assert.ok(c > b, "seq must keep increasing");
});

test("postPartial is a no-op (never throws) when runtime env is unset", async () => {
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
    await postPartial("Thinking about it", 7, { logger: { debug() {} } });
  } finally {
    if (prev.base !== undefined) process.env.NBHD_API_BASE_URL = prev.base;
    if (prev.tenant !== undefined) process.env.NBHD_TENANT_ID = prev.tenant;
    if (prev.key !== undefined) process.env.NBHD_INTERNAL_API_KEY = prev.key;
  }
});
