// Tests for the in-container cron sync (security-critical logic).
//   node --test runtime/openclaw/nbhd-cron-sync.test.mjs
//
// Env is set BEFORE the dynamic import so the module's module-level KEY /
// CRONS_FILE constants pick up the test values.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { writeFile, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const KEY = "test-internal-key-abc123";
const dir = await mkdtemp(path.join(tmpdir(), "nbhd-cron-sync-"));
const CRONS_FILE = path.join(dir, "nbhd-crons.json");
process.env.NBHD_INTERNAL_API_KEY = KEY;
process.env.NBHD_CRONS_FILE = CRONS_FILE;

const { isSafeJob, buildAddArgs, msToDuration, readSignedJobs, atFireMs, sameCron } = await import("./nbhd-cron-sync.mjs");

function signDoc(jobs, { badSig = false, tamper = false } = {}) {
  const signed = JSON.stringify(jobs);
  let sig = createHmac("sha256", KEY).update(signed).digest("hex");
  if (badSig) sig = "0".repeat(64);
  return JSON.stringify({ signed: tamper ? signed + " " : signed, sig });
}

test("isSafeJob: allow message crons, REFUSE shell", () => {
  assert.equal(isSafeJob({ payload: { kind: "agentTurn", message: "hi" } }), true);
  assert.equal(isSafeJob({ payload: { kind: "systemEvent" } }), true);
  assert.equal(isSafeJob({ payload: { kind: "command", command: "rm -rf /" } }), false);
  assert.equal(isSafeJob({ payload: { kind: "script", script: "x" } }), false);
  // agentTurn kind but a smuggled command/script field anywhere → refused
  assert.equal(isSafeJob({ payload: { kind: "agentTurn", message: "hi" }, command: "evil" }), false);
  assert.equal(isSafeJob({ payload: { kind: "agentTurn", message: "hi" }, commandArgv: ["x"] }), false);
  // a benign message that merely contains the words command/script is fine
  assert.equal(isSafeJob({ payload: { kind: "agentTurn", message: "run this command or script" } }), true);
  assert.equal(isSafeJob(null), false);
  assert.equal(isSafeJob({}), false);
});

test("msToDuration", () => {
  assert.equal(msToDuration(60000), "1m");
  assert.equal(msToDuration(1800000), "30m");
  assert.equal(msToDuration(3600000), "1h");
  assert.equal(msToDuration(86400000), "1d");
  assert.equal(msToDuration(604800000), "7d");
  assert.equal(msToDuration(500), null);
});

test("buildAddArgs: every + agentTurn → --no-deliver, never --command/--script", () => {
  const args = buildAddArgs({
    declarationKey: "nbhd:1", name: "R",
    schedule: { kind: "every", everyMs: 60000 },
    payload: { kind: "agentTurn", message: "ping" },
    delivery: { mode: "none" },
  });
  assert.equal(args[args.indexOf("--every") + 1], "1m");
  assert.ok(args.includes("--message"));
  assert.ok(args.includes("--no-deliver"));
  assert.ok(args.includes("--declaration-key") && args.includes("nbhd:1"));
  assert.ok(!args.includes("--command") && !args.includes("--script"));
});

test("buildAddArgs: cron + at variants carry tz", () => {
  const c = buildAddArgs({ declarationKey: "nbhd:2", schedule: { kind: "cron", expr: "0 9 * * *", tz: "Asia/Tokyo" }, payload: { kind: "agentTurn", message: "m" } });
  assert.ok(c.includes("--cron") && c.includes("0 9 * * *") && c.includes("--tz") && c.includes("Asia/Tokyo"));
  const a = buildAddArgs({ declarationKey: "nbhd:3", schedule: { kind: "at", at: "2026-09-17T12:00:00Z" }, payload: { kind: "agentTurn", message: "m" } });
  assert.ok(a.includes("--at") && a.includes("2026-09-17T12:00:00Z"));
  const aMs = buildAddArgs({ declarationKey: "nbhd:3b", schedule: { kind: "at", atMs: 1789646591000 }, payload: { kind: "agentTurn", message: "m" } });
  assert.ok(aMs.includes("--at") && aMs[aMs.indexOf("--at") + 1].endsWith("Z"));
});

test("buildAddArgs: announce delivery uses --announce, not --no-deliver", () => {
  const args = buildAddArgs({ declarationKey: "nbhd:4", schedule: { kind: "every", everyMs: 60000 }, payload: { kind: "agentTurn", message: "m" }, delivery: { mode: "announce", channel: "telegram" } });
  assert.ok(args.includes("--announce") && args.includes("--channel") && args.includes("telegram"));
  assert.ok(!args.includes("--no-deliver"));
});

test("buildAddArgs: null for unmappable / missing nbhd decl", () => {
  assert.equal(buildAddArgs({ declarationKey: "nbhd:x", schedule: { kind: "command" }, payload: { kind: "agentTurn", message: "m" } }), null);
  assert.equal(buildAddArgs({ name: "no-decl", schedule: { kind: "every", everyMs: 60000 }, payload: { kind: "agentTurn", message: "m" } }), null);
  assert.equal(buildAddArgs({ declarationKey: "nbhd:y", schedule: { kind: "every", everyMs: 60000 }, payload: { kind: "command" } }), null);
});

test("readSignedJobs: valid signature accepted", async () => {
  await writeFile(CRONS_FILE, signDoc([{ declarationKey: "nbhd:1", payload: { kind: "agentTurn", message: "hi" } }]));
  const jobs = await readSignedJobs();
  assert.ok(Array.isArray(jobs) && jobs.length === 1);
});

test("readSignedJobs: bad signature → null (refuse)", async () => {
  await writeFile(CRONS_FILE, signDoc([{ declarationKey: "nbhd:1" }], { badSig: true }));
  assert.equal(await readSignedJobs(), null);
});

test("readSignedJobs: tampered payload → null (sig mismatch)", async () => {
  await writeFile(CRONS_FILE, signDoc([{ declarationKey: "nbhd:1" }], { tamper: true }));
  assert.equal(await readSignedJobs(), null);
});

test("readSignedJobs: unsigned/plain jobs array → null", async () => {
  await writeFile(CRONS_FILE, JSON.stringify([{ declarationKey: "nbhd:1" }]));
  assert.equal(await readSignedJobs(), null);
});

test("atFireMs: parses ISO at, numeric atMs, rejects junk", () => {
  assert.equal(atFireMs({ at: "2026-09-18T07:43:00+09:00" }), Date.parse("2026-09-18T07:43:00+09:00"));
  assert.equal(atFireMs({ atMs: 1789646580000 }), 1789646580000);
  assert.equal(atFireMs({ at: "not-a-date" }), null);
  assert.equal(atFireMs({}), null);
  assert.equal(atFireMs(null), null);
});

test("sameCron: matches on owned fields, differs on schedule/message/delivery", () => {
  const base = { schedule: { kind: "cron", expr: "0 9 * * *", tz: "Asia/Tokyo" }, payload: { kind: "agentTurn", message: "hi" }, delivery: { mode: "none" } };
  // container adds runtime fields — still "same"
  const cur = { schedule: { kind: "cron", expr: "0 9 * * *", tz: "Asia/Tokyo", anchorMs: 123 }, payload: { kind: "agentTurn", message: "hi" }, delivery: { mode: "none" }, configRevision: "sha256:x" };
  assert.equal(sameCron(base, cur), true);
  assert.equal(sameCron(base, { ...cur, schedule: { ...cur.schedule, expr: "0 10 * * *" } }), false);
  assert.equal(sameCron(base, { ...cur, payload: { kind: "agentTurn", message: "changed" } }), false);
  assert.equal(sameCron(base, { ...cur, delivery: { mode: "announce" } }), false);
  // at crons compare by fire instant
  const atD = { schedule: { kind: "at", at: "2026-09-18T07:43:00+09:00" }, payload: { kind: "agentTurn", message: "m" }, delivery: { mode: "none" } };
  const atC = { schedule: { kind: "at", at: "2026-09-17T22:43:00Z" }, payload: { kind: "agentTurn", message: "m" }, delivery: { mode: "none" } };
  assert.equal(sameCron(atD, atC), true); // same instant, different notation
});
