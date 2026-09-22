// Tests for the in-container cron sync (security-critical logic).
//   node --test runtime/openclaw/nbhd-cron-sync.test.mjs
//
// Env is set BEFORE the dynamic import so the module's module-level KEY /
// CRONS_FILE constants pick up the test values.
import { after, test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { writeFile, mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const KEY = "test-internal-key-abc123";
const dir = await mkdtemp(path.join(tmpdir(), "nbhd-cron-sync-"));
after(() => rm(dir, { recursive: true, force: true }));
const CRONS_FILE = path.join(dir, "nbhd-crons.json");
process.env.NBHD_INTERNAL_API_KEY = KEY;
process.env.NBHD_CRONS_FILE = CRONS_FILE;

const { isSafeJob, buildAddArgs, sameCron, reconcileOnce, msToDuration, atFireMs, readSignedJobs } = await import("./nbhd-cron-sync.mjs");

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

test("atFireMs: numeric and ISO times; invalid or absent values", () => {
  const iso = "2026-09-20T12:00:00Z";
  const ms = Date.parse(iso);
  assert.equal(atFireMs({ atMs: ms }), ms);
  assert.equal(atFireMs({ at: ms }), ms);
  assert.equal(atFireMs({ at: iso }), ms);
  assert.equal(atFireMs({ atMs: ms, at: ms + 1000 }), ms);
  assert.equal(atFireMs({ atMs: 0 }), 0);
  for (const schedule of [undefined, null, "invalid", {}, { at: "invalid" }, { at: null }, { atMs: NaN }]) {
    assert.equal(atFireMs(schedule), null);
  }
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

const typedJob = () => ({
  declarationKey: "nbhd:typed", name: "Typed reminder", agentId: "main",
  schedule: { kind: "every", everyMs: 60000 },
  payload: {
    kind: "agentTurn", message: "Send the reminder\nThen stop.",
    model: "v4-flash", fallbacks: ["v4-pro", "provider/backup"],
    timeoutSeconds: 90, toolsAllow: ["nbhd_send_to_user", "read"], lightContext: true,
  },
  description: 'nbhd.v1 {"v":1,"check":{"kind":"contains","text":"東京"}}',
  delivery: { mode: "none" },
});

test("buildAddArgs: each supported typed field uses its individual CLI flag", () => {
  const job = typedJob();
  const args = buildAddArgs(job);
  for (const [flag, value] of [
    ["--model", "v4-flash"], ["--fallbacks", "v4-pro,provider/backup"],
    ["--timeout-seconds", "90"], ["--tools", "nbhd_send_to_user,read"],
    ["--description", job.description], ["--agent", "main"],
  ]) {
    assert.ok(args.includes(flag));
    assert.equal(args[args.indexOf(flag) + 1], value);
  }
  assert.ok(args.includes("--light-context"));
  assert.ok(!args.includes("--json"));
});

test("buildAddArgs: absent fields omitted; explicit false and empty tools are add CLI gaps", () => {
  const job = typedJob();
  job.payload = { kind: "agentTurn", message: "hi" };
  delete job.description;
  delete job.agentId;
  const args = buildAddArgs(job);
  for (const flag of ["--model", "--fallbacks", "--timeout-seconds", "--tools", "--light-context", "--no-light-context", "--description", "--agent"]) {
    assert.ok(!args.includes(flag));
  }
  job.payload.lightContext = false;
  assert.deepEqual(buildAddArgs(job), args);
  job.payload.fallbacks = [];
  const emptyFallbacks = buildAddArgs(job);
  assert.equal(emptyFallbacks[emptyFallbacks.indexOf("--fallbacks") + 1], "");
  job.payload.toolsAllow = [];
  assert.equal(buildAddArgs(job), null);
});

test("buildAddArgs: parameters never enable a command/script payload or field", () => {
  for (const kind of ["command", "script", "unknown"]) {
    const job = typedJob();
    job.payload.kind = kind;
    assert.equal(buildAddArgs(job), null);
  }
  for (const key of ["command", "commandArgv", "command_argv", "commandInput", "commandCwd", "commandEnv", "script"]) {
    const job = typedJob();
    job.payload.nested = { [key]: "evil" };
    assert.equal(isSafeJob(job), false);
    assert.equal(buildAddArgs(job), null);
  }
  const event = typedJob();
  event.payload.kind = "systemEvent";
  const args = buildAddArgs(event);
  assert.ok(args.includes("--system-event"));
  for (const flag of ["--model", "--fallbacks", "--timeout-seconds", "--tools", "--light-context", "--command", "--script"]) {
    assert.ok(!args.includes(flag));
  }
});

for (const [field, changed] of [
  ["model", "v4-pro"], ["fallbacks", ["v4-pro"]], ["timeoutSeconds", 120],
  ["toolsAllow", ["nbhd_send_to_user"]], ["lightContext", false],
]) {
  test(`sameCron: ${field}-only changes and removal require an upsert`, () => {
    const current = typedJob();
    const desired = structuredClone(current);
    assert.equal(sameCron(current, desired), true);
    desired.payload[field] = changed;
    assert.equal(sameCron(current, desired), false);
    delete desired.payload[field];
    assert.equal(sameCron(current, desired), false);
  });
}

test("sameCron: contract-only change detected; runtime state ignored", () => {
  const current = typedJob();
  const desired = typedJob();
  current.id = "runtime-id";
  current.state = { nextRunAtMs: 99999 };
  current.schedule.anchorMs = 1234;
  current.wakeMode = "now";
  assert.equal(sameCron(current, desired), true);
  desired.description += " ";
  assert.equal(sameCron(current, desired), false);
  delete desired.description;
  assert.equal(sameCron(current, desired), false);
  assert.equal(sameCron(undefined, desired), false);
});

test("sameCron: unpinned wake mode ignores the container default; pinned differences matter", () => {
  const current = typedJob();
  const desired = typedJob();
  current.wakeMode = "next-heartbeat";
  assert.equal(sameCron(current, desired), true);
  desired.wakeMode = null;
  assert.equal(sameCron(current, desired), true);
  desired.wakeMode = "now";
  assert.equal(sameCron(current, desired), false);
  desired.wakeMode = "next-heartbeat";
  assert.equal(sameCron(current, desired), true);
});

test("sameCron: fallback order and explicit empty override are significant", () => {
  const current = typedJob();
  const desired = typedJob();
  desired.payload.fallbacks.reverse();
  assert.equal(sameCron(current, desired), false);
  current.payload.fallbacks = [];
  delete desired.payload.fallbacks;
  assert.equal(sameCron(current, desired), false);
});

test("reconcileOnce: unchanged skips add; fallback-only change applies; failed add is retained", async () => {
  const desired = typedJob();
  await writeFile(CRONS_FILE, signDoc([desired]));
  const current = { ...typedJob(), id: "runtime-id" };
  const calls = [];
  let failAdd = false;
  const run = async args => {
    calls.push(args);
    if (args[1] === "list") return JSON.stringify({ jobs: [current, { id: "operator", declarationKey: "operator:keep" }] });
    if (args[1] === "add" && failAdd) throw new Error("synthetic failure");
    return "{}";
  };
  assert.equal((await reconcileOnce({ run })).applied, 0);
  assert.deepEqual(calls.map(args => args[1]), ["list"]);
  current.payload.fallbacks = [];
  calls.length = 0;
  assert.equal((await reconcileOnce({ run })).applied, 1);
  assert.deepEqual(calls.map(args => args[1]), ["list", "add"]);
  assert.ok(calls[1].includes("v4-pro,provider/backup"));
  failAdd = true;
  calls.length = 0;
  assert.equal((await reconcileOnce({ run })).removed, 0);
  assert.deepEqual(calls.map(args => args[1]), ["list", "add"]);
});

test("reconcileOnce: past or undetermined one-shots absent from current are skipped", async t => {
  const now = Date.parse("2026-09-20T12:00:00Z");
  t.mock.method(Date, "now", () => now);
  for (const time of [
    { at: new Date(now - 1000).toISOString() },
    { at: now - 1000 }, { atMs: now - 1000 },
    { at: now }, { at: "invalid" }, {},
  ]) {
    const job = typedJob();
    job.schedule = { kind: "at", ...time };
    await writeFile(CRONS_FILE, signDoc([job]));
    const calls = [];
    const run = async args => {
      calls.push(args);
      return JSON.stringify({ jobs: [] });
    };
    // A stale signed file must remain harmless across repeated polls.
    for (let poll = 0; poll < 2; poll++) {
      assert.deepEqual(await reconcileOnce({ run }), { applied: 0, removed: 0, skipped: 1, ok: true });
    }
    assert.deepEqual(calls, [["cron", "list", "--json"], ["cron", "list", "--json"]]);
  }
});

test("reconcileOnce: future one-shot absent from current is added", async t => {
  const now = Date.parse("2026-09-20T12:00:00Z");
  t.mock.method(Date, "now", () => now);
  const at = new Date(now + 60000).toISOString();
  const job = typedJob();
  job.schedule = { kind: "at", at };
  await writeFile(CRONS_FILE, signDoc([job]));
  const calls = [];
  const result = await reconcileOnce({ run: async args => {
    calls.push(args);
    return JSON.stringify({ jobs: [] });
  } });
  assert.deepEqual(result, { applied: 1, removed: 0, skipped: 0, ok: true });
  assert.deepEqual(calls.map(args => args[1]), ["list", "add"]);
  assert.equal(calls[1][calls[1].indexOf("--at") + 1], at);
});

test("reconcileOnce: signature and kind controls hold; removals stay in nbhd namespace", async () => {
  const unsafe = typedJob();
  unsafe.payload.command = "evil";
  await writeFile(CRONS_FILE, signDoc([unsafe]));
  const calls = [];
  const run = async args => {
    calls.push(args);
    return JSON.stringify({ jobs: [
      { id: "stale", declarationKey: "nbhd:stale" },
      { id: "keep", declarationKey: "operator:keep" },
    ] });
  };
  const result = await reconcileOnce({ run });
  assert.equal(result.skipped, 1);
  assert.equal(result.applied, 0);
  assert.deepEqual(calls, [["cron", "list", "--json"], ["cron", "rm", "stale"]]);
  await writeFile(CRONS_FILE, signDoc([typedJob()], { badSig: true }));
  calls.length = 0;
  assert.equal((await reconcileOnce({ run })).ok, false);
  assert.deepEqual(calls, []);
});

test("reconcileOnce: list failure makes no mutations", async () => {
  await writeFile(CRONS_FILE, signDoc([typedJob()]));
  const calls = [];
  const result = await reconcileOnce({ run: async args => {
    calls.push(args);
    throw new Error("synthetic list failure");
  } });
  assert.equal(result.ok, false);
  assert.deepEqual(calls, [["cron", "list", "--json"]]);
});

test("pinned 9.4 source registers each emitted flag; negative light context is edit-only", async () => {
  const root = process.env.OPENCLAW_PACKAGE_ROOT || "/tmp/openclaw-src-9.4/package";
  const source = await readFile(path.join(root, "dist/cron-cli-BTI9dsDQ.mjs"), "utf8");
  const start = source.indexOf("function registerCronMutationOptions(");
  const end = source.indexOf("\n}", start);
  const options = source.slice(start, end);
  for (const flag of ["--model <model>", "--fallbacks <list>", "--timeout-seconds <n>", "--tools <list>", "--light-context", "--description <text>", "--agent <id>"]) {
    assert.ok(options.includes(`.option("${flag}"`), flag);
  }
  assert.ok(!options.includes('"--no-light-context"'));
  assert.ok(source.includes('.option("--no-light-context", "Disable lightweight bootstrap context for agent jobs")'));
  assert.ok(source.includes("lightContext: opts.lightContext === true ? true : void 0"));
});
