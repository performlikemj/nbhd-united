// Spec for daily-note ISO date helpers. Pure functions only — runnable with
// Node's built-in runner (`node --test`) after a tsc transpile, no test
// framework dependency required (mirrors authorize-decision.test.ts).
import { test } from "node:test";
import assert from "node:assert/strict";

import { isISODate, shiftISODate, todayISO } from "./journal-date";

test("isISODate accepts real calendar dates", () => {
  assert.equal(isISODate("2026-06-28"), true);
  assert.equal(isISODate("2000-01-01"), true);
  assert.equal(isISODate("2024-02-29"), true); // 2024 is a leap year
});

test("isISODate rejects shape mismatches and the NaN slug", () => {
  for (const bad of ["daily", "", "NaN-NaN-NaN", "2026-6-1", "2026/06/28", "june", null, undefined]) {
    assert.equal(isISODate(bad as string), false, `expected ${String(bad)} to be invalid`);
  }
});

test("isISODate rejects impossible dates via Date round-trip", () => {
  assert.equal(isISODate("2026-13-40"), false);
  assert.equal(isISODate("2026-02-30"), false);
  assert.equal(isISODate("2026-00-10"), false);
});

test("shiftISODate shifts a valid date and never emits NaN", () => {
  assert.equal(shiftISODate("2026-06-28", -1), "2026-06-27");
  assert.equal(shiftISODate("2026-06-28", 1), "2026-06-29");
  // Month/year boundaries
  assert.equal(shiftISODate("2026-03-01", -1), "2026-02-28");
  assert.equal(shiftISODate("2026-12-31", 1), "2027-01-01");
});

test("shiftISODate on a non-date slug falls back to today (the NaN-NaN-NaN guard)", () => {
  for (const bad of ["daily", "", "NaN-NaN-NaN"]) {
    const out = shiftISODate(bad, -1);
    assert.ok(!out.includes("NaN"), `shiftISODate(${JSON.stringify(bad)}) leaked NaN: ${out}`);
    assert.ok(isISODate(out), `shiftISODate(${JSON.stringify(bad)}) produced non-ISO: ${out}`);
  }
});

test("todayISO is a valid ISO date", () => {
  assert.ok(isISODate(todayISO()));
});
