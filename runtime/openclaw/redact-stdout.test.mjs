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
  const input = '[tools] {"event":"x","message":"private user note"}';
  const out = redactor.redactLine(input);
  assert.match(out, /"message":"\*\*\*"/);
  assert.doesNotMatch(out, /private user note/);
});

test("Strategy A: long message is masked completely", () => {
  const longContent = "x".repeat(40);
  const input = `[tools] {"message":"${longContent}"}`;
  const out = redactor.redactLine(input);
  assert.match(out, /"message":"\*\*\*"/);
  assert.doesNotMatch(out, /xxxx/);
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

// Gateway-fatal config diagnostics MUST survive redaction — they were being
// dropped as non-operational during the 2026-07-05 incident, blinding
// diagnosis while a schema-invalid openclaw.json kept the gateway down.
const gatewayFatalCases = [
  // The ACTUAL 2026-07-05 error (was being truncated into a misleading
  // `agents.defaults: Invalid input`).
  "plugins.load.paths: plugin: plugin path not found: /opt/nbhd/plugins/nbhd-friends-tools",
  "plugin path not found: /opt/nbhd/plugins/nbhd-friends-tools",
  "agents.defaults: Invalid input",
  "agents.defaults.model.primary: Required",
  "plugins.entries: Unrecognized key(s) in object: 'foo'",
  "Gateway failed to start: Invalid config at /home/node/.openclaw/openclaw.json",
  "Invalid config at /home/node/.openclaw/openclaw.json",
  "Error: Gateway config validation failed",
];

for (const line of gatewayFatalCases) {
  test(`gateway-fatal config error survives redaction: ${JSON.stringify(line.slice(0, 60))}`, () => {
    assert.equal(redactor.looksGatewayFatal(line), true);
    // Every gatewayFatalCases line is secret-free, so the maskBareSecrets
    // passthrough must leave it byte-identical.
    assert.equal(redactor.redactLine(line), line);
  });
}

// Config echoes and arbitrary fatal-message suffixes are not diagnostics.
test("gateway-fatal prose and config echoes fail closed", () => {
  for (const line of [
    'Invalid config: {"gateway":{"auth":{"token":"synthetic-credential"}}}',
    'Gateway failed to start: upstream rejected Bearer abcdefgh12345678 handshake',
  ]) assert.match(redactor.redactLine(line), /non-operational line dropped/);
});

test("gateway-fatal passthrough leaves a secret-free Zod verdict unchanged", () => {
  const line = "agents.defaults.model.primary: Required";
  assert.equal(redactor.redactLine(line), line);
});

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

// Synthetic regression table: every prefix in the supplied seven-day oc-*
// survey, plus runtime call sites. No tenant log content is used.
const shapeCases = [
  ['Here is your update', false],
  ['Still working on your request', false],
  ['Ping! Your update is ready', false],
  ['Got your message', false],
  ['🏡 Your neighborhood update', false],
  ['3am is a good time', false],
  ['[tool policy] private reply', false],
  ['[Tool-policy] private reply', false],
  ['[gateway]private reply', false],
  ['[25:0x35216ABC] private reply', false],
  [`[a${'b'.repeat(41)}] private reply`, false],
  [`[a${'b'.repeat(40)}] status=ok`, true],
  ['[agents/tool-policy] visible_tools=12 policy=chat', true],
  ['[tasks/registry] registered=4', true],
  ['[shutdown] signal=SIGTERM', true],
  ['[gmail-watcher] status=disabled', true],
  ['[entrypoint] starting gateway', true],
  ['[tools-invoke] status=ok', true],
  ['[agent/embedded] run started', true],
  ['[health-monitor] status=healthy', true],
  ['[heartbeat] interval=30s', true],
  ['[diagnostic] queue_depth=0', true],
  ['[nbhd:redact] non-operational line dropped (12 chars)', true],
  ['[ws] connected', true],
  ['[tools] registered=12', true],
  ['- openrouter/synthetic-model model configured, enabled automatically.', true],
  ['curl: (7) Failed to connect to localhost port 8080', true],
  ['rm: cannot remove /tmp/synthetic: Permission denied', true],
  ['cp: cannot stat /tmp/synthetic: No such file or directory', true],
  ['[25:0x35216000] allocation failure', true],
  ...['mv', 'mkdir', 'chmod', 'ln', 'sh', 'bash'].map((name) => [`${name}: synthetic startup error`, true]),
  ['- Rent $2,500 due tomorrow', false],
  ['Rent: $2,500 due tomorrow', false],
  ['[Note] your balance is $100', false],
  ['[Label] private reply', false],
  ['label: private reply', false],
  ['2026-09-08T11:48:49Z private reply', false],
  ['- Health/private diagnosis', false],
  ['I saw Invalid config in your notes', false],
  ['Gateway failed to start: private reply', false],
  ['Invalid config private reply', false],
  ['Read /home/node/.openclaw/openclaw.json for my private note', false],
  ['{"message":"private reply","balance":2500}', false],
  ['{"level":"info","logger":"unknown","message":"private reply"}', false],
  ['{"level":"info","logger":"gateway","extra":"private reply"}', false],
  ['{"level":"info","logger":"gateway","time":"private reply"}', false],
  ['{"level":"info","logger":"gateway","message":{"private":"reply"}}', false],
  ['[{"level":"info","message":"private reply"}]', false],
  ['{"level":"info",', false],
  ['Prose {"message":"private reply"} still private', false],
  ['prose raw_params={"message":"private reply"}', false],
  ['[gateway] listening on :18789', true],
  ['[proxy]   /telegram-webhook -> :8080', true],
  ['[nbhd:tools] registered tool=nbhd_send_to_user plugin=journal', true],
  ['[nbhd:tools] call nbhd_send_to_user id=synthetic', true],
  ['[nbhd:tools] ok nbhd_send_to_user id=synthetic duration=10ms', true],
  ['[nbhd:tools] error nbhd_send_to_user id=synthetic duration=10ms', true],
  ['[nbhd] chmod EPERM handler registered via OpenClaw plugin SDK', true],
  ['[subagent-bridge] delivered_by=parent status=ok runId=synthetic', true],
  ['[plugins] NBHD usage reporter plugin registered', true],
  ['[plugins] nbhd-routing-context: before_tool_call guard error: Error', true],
  ['2026-09-08T11:48:49Z [ws] connected', true],
  ['2026-09-08T11:48:49Z [Note] private reply', false],
  ['INFO httpx HTTP Request: POST https://example.invalid/health "HTTP/1.1 200 OK"', true],
  ['2026-09-08 11:48:49,123 INFO httpx HTTP Request: GET https://example.invalid/health "HTTP/1.1 200 OK"', true],
  ['INFO:     127.0.0.1:12345 - "GET /health HTTP/1.1" 200 OK', true],
  ['{"level":"info","logger":"gateway","time":123,"message":"private reply"}', true],
  ['{"level":30,"component":"plugins","event":"registered","msg":"private reply"}', true],
];
for (const [line, pass] of shapeCases) {
  test(`synthetic allowlist ${pass ? 'pass' : 'drop'}: ${line}`, () => {
    const out = redactor.redactLine(line);
    assert.equal(out === `[nbhd:redact] non-operational line dropped (${line.length} chars)`, !pass);
    assert.ok(!out.includes('private reply'));
  });
}

test('every survey prefix still masks content and credentials', () => {
  for (const [line, pass] of shapeCases) {
    if (!pass || !/^(\[|curl:|rm:|cp:|mv:|mkdir:|chmod:|ln:|sh:|bash:)/.test(line)) continue;
    const out = redactor.redactLine(line + ' {"message":"private reply","token":"private credential"}');
    assert.ok(!out.includes('private'));
    assert.ok(out.includes('"message":"***"'));
    assert.ok(out.includes('"token":"***"'));
  }
});

test('classifier exceptions drop the line and preserve its length counter', () => {
  const pattern = redactor.OPERATIONAL_LINE_PATTERNS[0];
  const original = pattern.test;
  try {
    pattern.test = () => { throw new Error('synthetic'); };
    assert.equal(redactor.redactLine('[gateway] private reply'),
      '[nbhd:redact] non-operational line dropped (23 chars)');
  } finally { pattern.test = original; }
});

test('stream decoding exceptions and unsupported chunks never echo input', () => {
  const writes = [];
  const stream = { write: (...args) => { writes.push(args); return true; } };
  redactor.wrapStream(stream);
  const cb = () => {};
  const chunk = { toString() { throw new Error('private reply'); } };
  assert.equal(stream.write(chunk, cb), true);
  stream.write(123);
  assert.equal(writes.length, 2);
  for (const [out] of writes) assert.match(out, /non-operational line dropped/);
  assert.equal(writes[0][2], cb);
});

test('deep or malformed tool parameters never leave a tail', () => {
  for (const suffix of ['{"nested":{"a":{"b":{"c":"private reply"}}}}', '{"message":"private reply"']) {
    assert.equal(redactor.redactLine('[tools] failed raw_params=' + suffix),
      '[tools] failed raw_params=<redacted>');
  }
});

test('JSON escaped content keys are masked after parsing', () => {
  const line = '{"level":"info","logger":"gateway","mess\\u0061ge":"private reply"}';
  assert.equal(JSON.parse(redactor.redactLine(line)).message, '***');
});

test('embedded JSON fields mask entire values, including escaped keys and containers', () => {
  for (const value of ['"private reply"', '["private reply"]', '{"nested":"private reply"}', '123', 'null']) {
    const line = '[tools] {"mess\\u0061ge":' + value + ',"status":500}';
    assert.equal(redactor.redactLine(line), '[tools] {"message":"***","status":500}');
  }
});

test('decoder returning a non-string cannot bypass classification', () => {
  let written;
  const stream = { write: (chunk) => { written = chunk; } };
  redactor.wrapStream(stream);
  stream.write({ toString: () => Buffer.from('private reply') });
  assert.match(written, /non-operational line dropped/);
});

test('credential fields retain no short values or quoted suffixes', () => {
  assert.equal(redactor.redactLine('[tools] {"password":"private phrase with spaces"}'),
    '[tools] {"password":"***"}');
  assert.equal(redactor.redactLine('[tools] token=abc'), '[tools] token=***');
});
