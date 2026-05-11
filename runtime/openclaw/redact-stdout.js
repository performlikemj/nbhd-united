'use strict';

// nbhd's stdout/stderr redaction sidecar for the OpenClaw container.
//
// Why this file exists:
// Container stdout/stderr ships to a shared Azure Log Analytics workspace.
// Two leak classes were observed in production:
//
//   1. STDERR — `[tools] X failed: ... raw_params={"...","message":"<user
//      text>"}` from `pi-tool-definition-adapter-B1yqvP_o.js:164` →
//      `logError → runtime.error → console.error`. Upstream's
//      `redactToolDetail` runs but its DEFAULT_REDACT_PATTERNS only mask
//      auth shapes, not free-form `"message":` fields.
//
//   2. STDOUT — bare assistant reply text written un-prefixed (e.g.
//      "Got it — I'll remind you in 10 minutes…"). Source not fully pinned
//      in `openclaw@2026.5.7` dist (most likely a `logInfo`/`logSuccess`
//      call site on the reply path). The gateway fast path
//      (`cli/run-main.js:155-184`) returns before `enableConsoleCapture()`
//      (line 408), so OpenClaw's own console wrap never installs.
//      See memory: project_openclaw_gateway_skips_console_capture.md.
//
// Layer-1 (this isn't it): `apps/orchestrator/config_generator.py`
// provides `logging.redactPatterns` so `redactToolDetail` masks the
// `"message":"..."` shape before the line crosses into stderr.
//
// Layer-2 (this file): catches everything that bypasses layer-1 by
// wrapping the lowest-level write methods on stdout and stderr. Two
// strategies per line:
//
//   (A) Pattern-based — mask known JSON-shape leaks (raw_params blob,
//       JSON content fields). Preserves the surrounding `[tools]` error
//       message so ops still sees what failed.
//
//   (B) Operational-line classifier — for the bare-prose stdout leak.
//       Lines whose shape does NOT match a recognised operational
//       prefix get fully replaced with a marker indicating N chars were
//       redacted. Strict by design (per design review 2026-05-11):
//       operators who need to write diagnostic stdout should use a
//       known prefix shape (e.g. `[ops] message`).
//
// Wiring: loaded via `NODE_OPTIONS --require /opt/nbhd/redact-stdout.js`
// in `Dockerfile.openclaw`, same mechanism as `suppress-chmod-eperm.js`.
// Idempotent (`__nbhdRedacted` guard) so the `--require` re-firing for
// every node child process is safe.
//
// Fail-open: any throw inside redactLine() passes the original write
// through unmodified. Logs must never break because of the redactor.
//
// What this file deliberately does NOT do:
//   - Wrap console.* directly (process.{stdout,stderr}.write is the
//     ultimate destination for all of them).
//   - Buffer partial-line writes. The leaks observed in canary all emit
//     full lines per write. Multi-write line assembly would add state
//     and complexity for marginal coverage.
//   - Strip ANSI before pattern matching. The leaked content shapes
//     observed in production didn't carry color codes; if a future
//     emission does, the worst case is a missed redaction (fail-open).
//
// Verification recipe (on every OpenClaw version bump):
//   1. After deploying, query Log Analytics on a tenant container for
//      anything that should have been redacted:
//        ContainerAppConsoleLogs_CL
//        | where TimeGenerated > ago(2h)
//        | where ContainerAppName_s == "oc-<...>"
//        | where Log_s startswith "[tools]" and Log_s contains "raw_params="
//      → raw_params values should appear as masked tokens, NOT user text.
//   2. Same query without the [tools] filter, looking at stdout for
//      non-prefixed prose — should see `[nbhd:redact]` markers, not
//      assistant reply text.

// ─────────────────────────────────────────────────────────────────────────
// Strategy A — pattern-based field redaction
// ─────────────────────────────────────────────────────────────────────────

// Mask token. Mirrors the shape of upstream's `maskToken` (first 6 + …
// + last 4 chars) so masked values look familiar to operators reading
// the existing logs. Short values get `***`.
function maskToken(token) {
  if (typeof token !== 'string' || token.length < 18) return '***';
  return `${token.slice(0, 6)}…${token.slice(-4)}`;
}

// JSON `"field":"value"` shapes that carry tenant-supplied content. Run
// FIRST so the inner field gets masked before broader heuristics kick in.
// The capture group is the value (without surrounding quotes); we replace
// only that group's content.
const JSON_FIELD_RE =
  /"(message|text|content|prompt|response|reply|body|caption|user_text|userText|assistantText)"\s*:\s*"((?:[^"\\]|\\.)*)"/g;

// Bare `raw_params={...}` / `effective_params={...}` blocks. Greedy with
// brace balancing via a single nested-brace level (covers the observed
// canary leak which has up to 3 levels — the outermost ‘job’ wrapper). If
// nesting goes deeper, the regex will not consume the whole blob; the
// remaining tail will then be processed by JSON_FIELD_RE on the second
// pass. This is acceptable: it's belt-and-braces with JSON_FIELD_RE.
const TOOL_PARAMS_BLOB_RE =
  /(raw_params|effective_params)=\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}/g;

function applyFieldPatterns(line) {
  let out = line;
  // Inner-field masks first.
  out = out.replace(JSON_FIELD_RE, (match, fieldName, value) => {
    return `"${fieldName}":"${maskToken(value)}"`;
  });
  // Then collapse any params blob we still recognise to a single token.
  out = out.replace(TOOL_PARAMS_BLOB_RE, (match, key) => `${key}=<redacted>`);
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// Strategy B — operational-line classifier
// ─────────────────────────────────────────────────────────────────────────
//
// Lines that match ANY of these patterns are considered operational and
// pass through unmodified (after strategy A's field-level masking). Lines
// that match NONE are assumed to be tenant content and get replaced with
// a marker.
//
// Patterns derived from a survey of canary stdout/stderr (2026-05-11):
//   - `[gateway] http server listening...`            → bracket prefix
//   - `[ws] ⇄ res ✓ cron.list 109ms conn=...`         → bracket prefix
//   - `[plugins] NBHD usage reporter plugin registered` → bracket prefix
//   - `[nbhd] chmod EPERM suppressed for ...`         → bracket prefix
//   - `2026-05-11T07:15:45.686+00:00 [gateway] ...`   → ISO + bracket
//   - `tools-invoke: tool execution failed: ...`      → subsystem prefix
//   - `Gateway target: ws://127.0.0.1:18789`          → key: value pair
//   - `Source: local loopback`                        → key: value pair
//   - `Config: /home/node/.openclaw/openclaw.json`    → key: value pair
//   - `- openrouter/moonshotai/kimi-k2.6 model ...`   → manifest list line
//
// And what is NOT operational:
//   - `Got it — I'll remind you in 10 minutes...`     → assistant reply
//   - `**Hip Abductor**`                              → markdown formatting
//   - `| Exercise | Sets × Reps | Weight |`           → markdown table
//   - `Run **after** legs, not before.`               → prose
//
// Order: cheapest checks first.
const OPERATIONAL_LINE_PATTERNS = [
  // Bracket prefix: `[anything] rest of line`
  /^\[[A-Za-z0-9_./:-]+\]/,
  // ISO 8601 timestamp at start of line
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/,
  // Subsystem prefix: `lowercase-name: rest` (matches `tools-invoke:`,
  // `client-tools-channels:`, etc., per the convention in openclaw
  // `logger-DksTYIAF.js#subsystemPrefixRe`)
  /^[a-z][a-z0-9-]{1,20}:\s/,
  // Banner-style key/value: `Capital[a-zA-Z]{1,20}: value` with a
  // non-space value. Catches "Gateway target: ws://...", "Config: ...",
  // "Bind: loopback". Conservative — requires the line to END with the
  // value so it can't accidentally pass a long prose line starting with
  // a Capital word + colon.
  /^[A-Z][A-Za-z]{0,20}(?:\s[A-Za-z]+)?:\s\S[^\n]{0,200}$/,
  // OpenClaw startup manifest lines: `- some/identifier ... configured,
  // enabled automatically.` etc.
  /^- [A-Za-z][\w/.-]+ /,
  // npm / node warnings + node deprecation lines, which legitimately
  // arrive on stderr without a structured prefix.
  /^\(node:\d+\)/,
  /^npm (?:warn|notice|info|error)\b/,
];

function looksOperational(line) {
  for (const re of OPERATIONAL_LINE_PATTERNS) {
    if (re.test(line)) return true;
  }
  return false;
}

// ─────────────────────────────────────────────────────────────────────────
// Per-line redaction pipeline
// ─────────────────────────────────────────────────────────────────────────

function redactLine(line) {
  if (line.length === 0) return line;

  // Strategy A: known JSON field shapes. If applied, the line is by
  // definition operational (it had a recognised structure), so return it
  // without running the classifier.
  const masked = applyFieldPatterns(line);
  if (masked !== line) return masked;

  // Strategy B: classifier. Operational lines pass; anything else gets
  // replaced with a marker indicating the original length.
  if (looksOperational(line)) return line;

  return `[nbhd:redact] non-operational line dropped (${line.length} chars)`;
}

function redactChunk(text) {
  if (typeof text !== 'string' || text.length === 0) return text;

  // Process line-by-line. Preserve a trailing empty element so the join
  // re-emits the trailing newline if the original had one.
  const lines = text.split('\n');
  const lastIndex = lines.length - 1;
  for (let i = 0; i <= lastIndex; i += 1) {
    const isTrailingNewlineSentinel = i === lastIndex && lines[i] === '';
    if (isTrailingNewlineSentinel) continue;
    lines[i] = redactLine(lines[i]);
  }
  return lines.join('\n');
}

// ─────────────────────────────────────────────────────────────────────────
// Stream patching
// ─────────────────────────────────────────────────────────────────────────

function wrapStream(stream) {
  if (!stream || typeof stream.write !== 'function') return;
  if (stream.write.__nbhdRedacted) return;

  const orig = stream.write.bind(stream);

  stream.write = function nbhdRedactedWrite(chunk, encoding, cb) {
    try {
      let text;
      if (typeof chunk === 'string') {
        text = chunk;
      } else if (chunk && typeof chunk === 'object' && typeof chunk.toString === 'function') {
        // Buffer / Uint8Array
        text = chunk.toString(typeof encoding === 'string' ? encoding : 'utf8');
      } else {
        return orig(chunk, encoding, cb);
      }
      const redacted = redactChunk(text);
      return orig(redacted, typeof encoding === 'string' ? encoding : undefined, cb);
    } catch {
      // Fail-open: never break logging because of the redactor.
      return orig(chunk, encoding, cb);
    }
  };

  stream.write.__nbhdRedacted = true;
}

// Auto-install on load. Tests that need to import the redactor without
// patching streams (so the test runner's own output stays visible) set
// `NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1` before `require()`.
if (process.env.NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL !== '1') {
  wrapStream(process.stdout);
  wrapStream(process.stderr);

  // Emit a single startup confirmation on stderr — bracket-prefixed so
  // the classifier passes it through. Visible in container logs as
  // evidence that the redactor installed correctly.
  try {
    if (!global.__nbhdRedactStdoutAnnounced) {
      global.__nbhdRedactStdoutAnnounced = true;
      process.stderr.write('[nbhd:redact] stdout/stderr redaction installed\n');
    }
  } catch {
    // Don't crash on stderr write failures during boot.
  }
}

// Exported for unit testing — these are NOT used by the loader path,
// which only relies on the side-effectful stream wrap above.
module.exports = {
  redactLine,
  redactChunk,
  looksOperational,
  applyFieldPatterns,
  maskToken,
  wrapStream,
  OPERATIONAL_LINE_PATTERNS,
};
