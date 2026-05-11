// Tests for the stdout/stderr redaction sidecar.
//
// Run: NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1 node --test \
//        runtime/openclaw/redact-stdout.test.mjs
//
// The env var is REQUIRED — without it, the redactor wraps the test
// runner's own stdout/stderr and the test framework's TAP output gets
// redacted (lines like `ok 1 - …` don't match any operational pattern
// and would be replaced with `[nbhd:redact] non-operational line dropped`).

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";
import test from "node:test";

import { createRequire } from "node:module";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const require = createRequire(import.meta.url);

// Loading the redactor must NOT patch streams — see env var note above.
// The harness sets it; this assert catches a future change that breaks
// the test setup before any test ran.
assert.equal(
  process.env.NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL,
  "1",
  "Run this file with NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1 — otherwise the test runner's own output gets redacted.",
);

const redactor = require("./redact-stdout.js");

// ─────────────────────────────────────────────────────────────────────────
// Pattern-based redaction (Strategy A)
// ─────────────────────────────────────────────────────────────────────────

test("Strategy A: [tools] failed line with raw_params blob is collapsed", () => {
  // The exact leak captured on canary 2026-05-11T07:17:02 (stderr).
  const leak =
    '[tools] cron failed: invalid cron.add params: delivery.channel is ' +
    "required when multiple channels are configured: line, telegram " +
    'raw_params={"action":"add","job":{"enabled":true,"name":"Record ' +
    'SCORM Cloud Demo","schedule":{"kind":"at","at":"2026-05-11T07:38:' +
    '03.023Z"},"payload":{"kind":"agentTurn","message":"Reminder: record ' +
    'your SCORM Cloud training demo"}}}';

  const out = redactor.redactLine(leak);

  // Surrounding context (what ops needs) stays visible.
  assert.match(out, /^\[tools\] cron failed: invalid cron.add params:/);
  assert.match(out, /delivery\.channel is required/);
  // Sensitive payload is masked.
  assert.match(out, /raw_params=<redacted>/);
  assert.doesNotMatch(out, /Record SCORM Cloud Demo/);
  assert.doesNotMatch(out, /Reminder: record/);
});

test("Strategy A: web_fetch URL leak (file:// path) is collapsed", () => {
  const leak =
    '[tools] web_fetch failed: Invalid URL: must be http or https ' +
    'raw_params={"url":"file:///home/node/.openclaw/workspace/memory/2026-05-11.md"}';
  const out = redactor.redactLine(leak);
  assert.match(out, /^\[tools\] web_fetch failed:/);
  assert.match(out, /raw_params=<redacted>/);
  assert.doesNotMatch(out, /workspace\/memory/);
});

test("Strategy A: bare JSON message field outside raw_params is also masked", () => {
  // Hypothetical: a logger that emits `{... "message":"user text" ...}`
  // outside the raw_params= shape. The JSON_FIELD_RE catches it.
  const input = 'some prefix: {"event":"x","message":"private user note"}';
  const out = redactor.redactLine(input);
  assert.match(out, /"message":"\*\*\*"/);
  assert.doesNotMatch(out, /private user note/);
});

test("Strategy A: long enough message gets prefix…suffix mask, not ***", () => {
  const longContent = "x".repeat(40);
  const input = `prefix: {"message":"${longContent}"}`;
  const out = redactor.redactLine(input);
  assert.match(out, /"message":"x{6}…x{4}"/, "Long values should mask first 6 + … + last 4");
});

// ─────────────────────────────────────────────────────────────────────────
// Operational-line classifier (Strategy B)
// ─────────────────────────────────────────────────────────────────────────

const operationalCases = [
  "[gateway] http server listening",
  "[ws] ⇄ res ✓ cron.list 109ms conn=da2b4245…80d9 id=4000c058…5d38",
  "[plugins] NBHD usage reporter plugin registered",
  "[nbhd] chmod EPERM suppressed for /home/node/.openclaw/cron/jobs.json",
  "[nbhd:redact] stdout/stderr redaction installed",
  "2026-05-11T07:15:45.686+00:00 [gateway] loading configuration…",
  "tools-invoke: tool execution failed: GatewayTransportError",
  "client-tools-channels: handshake complete",
  "Gateway target: ws://127.0.0.1:18789",
  "Config: /home/node/.openclaw/openclaw.json",
  "Bind: loopback",
  "Source: local loopback",
  "- openrouter/moonshotai/kimi-k2.6 model configured, enabled automatically.",
  "(node:12) DeprecationWarning: foo is deprecated",
  "npm warn deprecated pkg@1.0.0: use newer",
];

for (const line of operationalCases) {
  test(`Strategy B keeps operational line: ${JSON.stringify(line.slice(0, 60))}`, () => {
    assert.equal(redactor.looksOperational(line), true);
    assert.equal(redactor.redactLine(line), line);
  });
}

const proseLeakCases = [
  "Got it — I'll remind you in 10 minutes via LINE.",
  "**Hip Abductor**",
  "| Exercise | Sets × Reps | Weight |",
  "Run **after** legs, not before.",
  "What're you training today — squats, deads, or both?",
  "Looking forward to the lunge report. Strong work.",
  "Want me to set up any follow-up reminders?",
  "I learned to drink more water after that marathon training month",
];

for (const line of proseLeakCases) {
  test(`Strategy B drops prose: ${JSON.stringify(line.slice(0, 60))}`, () => {
    assert.equal(redactor.looksOperational(line), false);
    assert.match(redactor.redactLine(line), /^\[nbhd:redact\] non-operational line dropped/);
    // Length is reported so an operator can sanity-check there's not
    // something massive being silently swallowed.
    assert.match(redactor.redactLine(line), /\(\d+ chars\)$/);
  });
}

test("empty line passes through unchanged", () => {
  assert.equal(redactor.redactLine(""), "");
});

// ─────────────────────────────────────────────────────────────────────────
// Chunk handling (multi-line + trailing newline)
// ─────────────────────────────────────────────────────────────────────────

test("redactChunk handles multi-line prose chunk", () => {
  // The exact 3-line emission observed at 2026-05-11T07:17:18 (canary).
  const chunk =
    "Got it — I'll remind you in 10 minutes via LINE.\n\nWant me to set up any follow-up reminders?";
  const out = redactor.redactChunk(chunk);
  const lines = out.split("\n");
  assert.equal(lines.length, 3, "Three lines (prose, empty, prose) preserved as three");
  assert.match(lines[0], /^\[nbhd:redact\]/);
  assert.equal(lines[1], "", "Empty middle line preserved");
  assert.match(lines[2], /^\[nbhd:redact\]/);
});

test("redactChunk preserves trailing newline", () => {
  const input = "[gateway] foo\n";
  const out = redactor.redactChunk(input);
  assert.equal(out.endsWith("\n"), true);
  assert.equal(out, "[gateway] foo\n");
});

test("redactChunk keeps operational lines, drops interleaved prose", () => {
  const input =
    "[gateway] processing request\nGot it — done!\n[ws] ⇄ res ok\n";
  const out = redactor.redactChunk(input);
  const lines = out.split("\n");
  assert.equal(lines[0], "[gateway] processing request");
  assert.match(lines[1], /^\[nbhd:redact\]/);
  assert.equal(lines[2], "[ws] ⇄ res ok");
  // Index 3 is the trailing empty preserved by split.
  assert.equal(lines[3], "");
});

test("redactChunk handles Buffer input (toString fallback)", () => {
  // Not the exact path the wrap takes — that uses Buffer.toString with
  // a passed encoding — but redactChunk should only see strings. This
  // confirms its contract: non-string input is returned untouched.
  const buf = Buffer.from("[gateway] hello\n", "utf8");
  // redactChunk explicitly checks for string — Buffer skips.
  assert.equal(redactor.redactChunk(buf), buf);
});

// ─────────────────────────────────────────────────────────────────────────
// Stream wrap end-to-end (subprocess integration)
// ─────────────────────────────────────────────────────────────────────────
//
// Spawn a child node process with --require pointing at the redactor.
// The child prints both a prose line (should be redacted) and an
// operational line (should pass). We capture both streams and assert.

test("wrap end-to-end: spawned child redacts prose, keeps operational", () => {
  const redactorPath = join(__dirname, "redact-stdout.js");
  const script = `
    process.stdout.write("[gateway] operational line\\n");
    process.stdout.write("Got it — I'll remind you in 10 minutes.\\n");
    process.stderr.write("[tools] cron failed: foo raw_params={\\"message\\":\\"user text\\"}\\n");
  `;
  // Child must NOT inherit the test harness's auto-install skip — we
  // explicitly want the wrap to install in the child.
  const childEnv = { ...process.env };
  delete childEnv.NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL;
  const result = spawnSync(process.execPath, ["--require", redactorPath, "-e", script], {
    encoding: "utf8",
    env: childEnv,
  });
  assert.equal(result.status, 0, `child exited non-zero: ${result.stderr}`);

  // stdout: operational kept, prose dropped.
  const stdout = result.stdout;
  assert.match(stdout, /^\[gateway\] operational line$/m);
  assert.match(stdout, /^\[nbhd:redact\] non-operational line dropped/m);
  assert.doesNotMatch(stdout, /Got it — I'll remind/);

  // stderr: install banner + [tools] line with redacted raw_params.
  const stderr = result.stderr;
  assert.match(stderr, /\[nbhd:redact\] stdout\/stderr redaction installed/);
  assert.match(stderr, /^\[tools\] cron failed: foo raw_params=<redacted>$/m);
  assert.doesNotMatch(stderr, /user text/);
});

test("wrap is idempotent: second --require run doesn't double-patch", () => {
  const redactorPath = join(__dirname, "redact-stdout.js");
  const script = `process.stdout.write("[gateway] hello\\n");`;
  const childEnv = { ...process.env };
  delete childEnv.NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL;
  const result = spawnSync(
    process.execPath,
    ["--require", redactorPath, "--require", redactorPath, "-e", script],
    { encoding: "utf8", env: childEnv },
  );
  assert.equal(result.status, 0);
  // The line should pass through exactly once — no doubling, no extra
  // wrapping artefacts.
  const lines = result.stdout.split("\n").filter((l) => l.length > 0);
  assert.deepEqual(lines, ["[gateway] hello"]);
});
