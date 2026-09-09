'use strict';

// Tenant console output reaches a shared logging workspace. Admit only known
// operational shapes, mask entire content values, and fail closed on errors.
// Prefixes are a logging convention, not proof of provenance: new call sites
// must keep free text out of operational messages and use masked fields.
// Loaded by NODE_OPTIONS --require; each write is classified independently.

// ─────────────────────────────────────────────────────────────────────────
// Strategy A — pattern-based field redaction
// ─────────────────────────────────────────────────────────────────────────

// No prefix/suffix retention: even a few characters can disclose tenant text.
function maskToken() {
  return '***';
}

const CONTENT_FIELDS = new Set([
  'message', 'msg', 'text', 'content', 'prompt', 'response', 'reply', 'body',
  'caption', 'user_text', 'userText', 'assistantText',
  'token', 'apiKey', 'api_key', 'secret', 'password', 'authorization',
]);

// Find a complete JSON value, including nested arrays/objects and escaped
// quotes. Malformed/truncated values consume the suffix, never a partial mask.
function jsonValueEnd(line, start) {
  const stack = [];
  let quoted = false;
  for (let i = start; i < line.length; i += 1) {
    const char = line[i];
    if (quoted) {
      if (char === '\\') i += 1;
      else if (char === '"') {
        quoted = false;
        if (stack.length === 0) return i + 1;
      }
    } else if (char === '"') quoted = true;
    else if (char === '{' || char === '[') stack.push(char);
    else if (char === '}' || char === ']') {
      if (stack.length === 0) return i;
      const open = stack.pop();
      if ((open === '{') !== (char === '}')) return line.length;
      if (stack.length === 0) return i + 1;
    } else if (stack.length === 0 && (char === ',' || /\s/.test(char))) return i;
  }
  return line.length;
}

function applyFieldPatterns(line) {
  // Params may be arbitrarily nested or malformed. Drop the entire remaining
  // suffix rather than leave a content-bearing tail after a partial match.
  let out = line.replace(/(raw_params|effective_params)=.*$/g, (_match, key) => `${key}=<redacted>`);
  const keys = /"((?:[^"\\]|\\.)*)"\s*:\s*/g;
  let match;
  while ((match = keys.exec(out)) !== null) {
    const field = JSON.parse(`"${match[1]}"`);
    if (!CONTENT_FIELDS.has(field)) continue;
    const end = jsonValueEnd(out, keys.lastIndex);
    const replacement = `"${field}":"${maskToken()}"`;
    out = out.slice(0, match.index) + replacement + out.slice(end);
    keys.lastIndex = match.index + replacement.length;
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// Strategy B — operational shapes from the supplied seven-day prefix survey.
// ─────────────────────────────────────────────────────────────────────────
const COMPONENT = '(?:gateway|ws|plugins|tools|proxy|nbhd(?::[a-z0-9_-]+)?|subagent-bridge|cron|agent/embedded|diagnostic|heartbeat|health-monitor|browser/server|canvas|reload|telegram|line|bonjour)';
const TIMESTAMP = '\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?(?:Z|[+-]\\d{2}:\\d{2})';
const OPERATIONAL_LINE_PATTERNS = [
  /^\[[a-z][a-z0-9_:./-]{0,40}\] /,
  /^\[\d+:0x[0-9a-f]+\] /,
  /^(curl|rm|cp|mv|mkdir|chmod|ln|sh|bash): /,
  new RegExp(`^${TIMESTAMP} \\[${COMPONENT}\\](?: |$)`),
  /^(?:tools-invoke|client-tools-channels): /,
  /^Gateway target: wss?:\/\/(?:127\.0\.0\.1|localhost):\d+$/,
  /^Config: \/home\/node\/\.openclaw\/openclaw\.json$/,
  /^Bind: (?:loopback|lan|auto)$/,
  /^Source: local loopback$/,
  /^- [a-z0-9_.-]+\/[a-z0-9_./-]+ model configured, enabled automatically\.$/,
  /^\(node:\d+\) (?:\[DEP\d+\] )?(?:DeprecationWarning|ExperimentalWarning|Warning): /,
  /^npm (?:warn|notice|info|error) /,
  // Known Python logger plus an HTTP request/access record, never just a date.
  /^(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d+ )?(?:INFO|WARNING|ERROR)[: ]+httpx[: ]+HTTP Request: (?:GET|POST|PUT|PATCH|DELETE|HEAD) https?:\/\/\S+ "HTTP\/[\d.]+ \d{3} [A-Za-z ]+"$/,
  /^(?:INFO: +)?(?:\d{1,3}\.){3}\d{1,3}:\d+ - "(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) \/[^ "?]* HTTP\/[\d.]+" \d{3}(?: [A-Za-z ]+)?$/,
];

// Fatal diagnostics allow schema paths and fixed verdicts, not arbitrary prose
// containing 'Invalid config' or a mention of the config file.
const GATEWAY_FATAL_CONFIG_PATTERNS = [
  /^(?:plugins\.load\.paths: plugin: )?plugin path not found: \/opt\/nbhd\/plugins\/[a-z0-9_/-]+$/,
  /^(?:agents|plugins|gateway|tools|channels)(?:\.[a-zA-Z_]+|\[\d+\])*: (?:Invalid input|Required|Unrecognized key\(s\) in object: '[a-zA-Z_]+')$/,
  /^(?:Gateway failed to start: )?Invalid config at \/home\/node\/\.openclaw\/openclaw\.json$/,
  /^(?:Error: )?Gateway config (?:invalid|validation failed)$/,
];

const JSON_LOG_KEYS = new Set(['level', 'time', 'timestamp', 'logger', 'component', 'event', 'message', 'msg', 'text', 'content', 'pid', 'status', 'duration_ms']);
function looksOperationalJson(line) {
  if (!line.startsWith('{')) return false;
  const value = JSON.parse(line);
  if (!value || Array.isArray(value)) return false;
  if (!Object.keys(value).every((key) => JSON_LOG_KEYS.has(key))) return false;
  if (!['trace', 'debug', 'info', 'warn', 'error', 'fatal', 10, 20, 30, 40, 50, 60].includes(value.level)) return false;
  const logger = value.logger ?? value.component;
  if (typeof logger !== 'string' || !new RegExp(`^${COMPONENT}$`).test(logger)) return false;
  return Object.entries(value).every(([key, val]) => {
    if (['message', 'msg', 'text', 'content'].includes(key)) return typeof val === 'string';
    if (['time', 'timestamp'].includes(key)) return typeof val === 'number' || new RegExp(`^${TIMESTAMP}$`).test(val);
    if (['pid', 'status', 'duration_ms'].includes(key)) return typeof val === 'number';
    if (['logger', 'component'].includes(key)) return typeof val === 'string' && new RegExp(`^${COMPONENT}$`).test(val);
    if (key === 'event') return typeof val === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(val);
    return true;
  });
}

function looksOperational(line) {
  return OPERATIONAL_LINE_PATTERNS.some((re) => re.test(line)) || looksOperationalJson(line);
}

function looksGatewayFatal(line) {
  return GATEWAY_FATAL_CONFIG_PATTERNS.some((re) => re.test(line));
}

// Mask credentials on every admitted operational line.
function maskBareSecrets(line) {
  let out = line;
  // token/key/secret assignments — keep the key name, mask the value.
  out = out.replace(
    /\b(token|apiKey|api_key|secret|password|authorization)["']?\s*[:=]\s*["']?([^"'\s,}]+)/gi,
    (match, _key, value) => match.slice(0, match.length - value.length) + maskToken(value),
  );
  // Provider-key shapes (sk-ant-, sk-or-v1-, sk-proj-, tvly-).
  out = out.replace(/\b(sk-[A-Za-z0-9_-]{8,})\b/g, (_match, token) => maskToken(token));
  out = out.replace(/\b(tvly-[A-Za-z0-9_-]{8,})\b/g, (_match, token) => maskToken(token));
  // Bearer credentials — keep the scheme word, mask the credential.
  out = out.replace(
    /\b[Bb]earer\s+([A-Za-z0-9._~+/=-]{8,})\b/g,
    (match, cred) => match.slice(0, match.length - cred.length) + maskToken(cred),
  );
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// Per-line redaction pipeline
// ─────────────────────────────────────────────────────────────────────────

function droppedLine(length) {
  return `[nbhd:redact] non-operational line dropped (${length} chars)`;
}

function redactLine(line) {
  if (line.length === 0) return line;
  try {
    // Classification is mandatory even when field masking would succeed.
    if (/[\r\x00-\x08\x0b\x0c\x0e-\x1f]/.test(line)) return droppedLine(line.length);
    if (looksGatewayFatal(line) || looksOperational(line)) {
      if (line.startsWith('{')) {
        const value = JSON.parse(line);
        for (const key of ['message', 'msg', 'text', 'content']) {
          if (key in value) value[key] = maskToken(value[key]);
        }
        return JSON.stringify(value);
      }
      return maskBareSecrets(applyFieldPatterns(line));
    }
  } catch {
    // Malformed JSON, classifier/masking failures: never return tenant content.
  }
  return droppedLine(line.length);
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
        if (typeof text !== 'string') throw new TypeError('invalid decoded chunk');
      } else {
        return orig('[nbhd:redact] non-operational line dropped (unknown chars)\n', undefined, typeof encoding === 'function' ? encoding : cb);
      }
      const redacted = redactChunk(text);
      return orig(redacted, typeof encoding === 'string' ? encoding : undefined, typeof encoding === 'function' ? encoding : cb);
    } catch {
      // Fail closed, including chunk decoding failures.
      return orig('[nbhd:redact] non-operational line dropped (unknown chars)\n', undefined, typeof encoding === 'function' ? encoding : cb);
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
  looksGatewayFatal,
  maskBareSecrets,
  applyFieldPatterns,
  maskToken,
  wrapStream,
  OPERATIONAL_LINE_PATTERNS,
  GATEWAY_FATAL_CONFIG_PATTERNS,
};
