/**
 * Unit tests for buildGroundingInjection — the assembly behind the
 * before_prompt_build hook that decides what gets injected into a firing cron.
 *
 * Run with: node --test runtime/openclaw/plugins/nbhd-cron-enforcement/test.js
 *
 * Importing index.js is side-effect-free (it only defines functions + exports;
 * the hooks register only when register(api) is called).
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { buildGroundingInjection } from "./index.js";

describe("buildGroundingInjection", () => {
  it("injects nothing for a missing/empty entry (system cron or non-cron run)", () => {
    assert.equal(buildGroundingInjection(undefined), undefined);
    assert.equal(buildGroundingInjection({}), undefined);
    assert.equal(
      buildGroundingInjection({ grounding_rule: "", prompt_injection: "" }),
      undefined,
    );
  });

  it("injects the grounding rule for a freeform cron (rule only, no pattern)", () => {
    const out = buildGroundingInjection({
      grounding_rule: "GROUNDING: verify status via tools.",
    });
    assert.match(out, /GROUNDING: verify status via tools\./);
  });

  it("injects grounding rule THEN pattern injection for a typed cron", () => {
    const out = buildGroundingInjection({
      grounding_rule: "GROUND_RULE",
      prompt_injection: "PATTERN_RULE",
    });
    assert.match(out, /GROUND_RULE/);
    assert.match(out, /PATTERN_RULE/);
    assert.ok(out.indexOf("GROUND_RULE") < out.indexOf("PATTERN_RULE"));
  });

  it("injects only the pattern when there is no grounding rule (defensive)", () => {
    const out = buildGroundingInjection({
      grounding_rule: "",
      prompt_injection: "PATTERN_ONLY",
    });
    assert.match(out, /PATTERN_ONLY/);
  });
});
