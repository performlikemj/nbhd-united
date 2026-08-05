/**
 * Behavioral error-transport tests for every nbhd-* plugin that talks to the
 * NBHD runtime.
 *
 * Run with: node --test runtime/openclaw/plugins/error-transport.test.js
 *
 * WHY THIS EXISTS
 * ---------------
 * Every plugin's runtime-call function used to build its thrown error with the
 * naive pattern `const detail = asTrimmedString(normalized.detail)`. DRF and the
 * NBHD runtime views commonly answer a bad write with a validation ENVELOPE —
 * `{error, message, details[]}` — or with top-level field errors
 * (`{week_rating: ["This field is required."]}`). Neither lives under `detail`,
 * so the naive read silently dropped the entire body: the model saw
 * "NBHD runtime error 400: validation_failed" with no idea WHICH field was
 * wrong, and could not self-correct. It retried the same broken call.
 *
 * The fix is the shared `compactErrorDetail()` helper, inserted byte-identical
 * into all 13 plugins. This suite is the BEHAVIORAL half of the guard: it drives
 * each plugin's real registration path with a fake `api`, stubs `fetch` with an
 * adversarial error body, and asserts the message the model actually receives.
 * The structural half — that the helper text is identical across all 13 files,
 * and that no runtime-calling plugin is missing from the list — lives in
 * `apps/orchestrator/test_plugin_error_transport.py`.
 *
 * Deliberately end-to-end through `register()` → `execute()` rather than
 * unit-testing an exported helper: the helper is NOT exported (it must stay a
 * byte-identical private block), and a plugin whose call site still reads
 * `normalized.detail` would pass a helper-only test while shipping the bug.
 *
 * Fully self-contained: no network. `globalThis.fetch` is replaced for the
 * duration of each case and restored afterwards, and every case asserts the
 * stub was actually reached — a plugin that throws before the transport (bad
 * config, missing tool, arg-guard rejection) fails loudly instead of passing
 * silently on a message that never came from the error block.
 */

import { describe, it, after, afterEach } from "node:test";
import assert from "node:assert/strict";

// ── Runtime config the plugins read ─────────────────────────────────────────
// Every plugin resolves the base URL from `pluginConfig.apiBaseUrl` OR the env
// var, and the tenant/key from env only. We set both paths so no plugin's
// getRuntimeConfig() can throw "NBHD_* is required" before reaching fetch.
const API_BASE_URL = "https://runtime.error-transport.test";
const TENANT_ID = "tenant-error-transport";
const INTERNAL_KEY = "internal-error-transport-key";

const PREVIOUS_ENV = {
  NBHD_API_BASE_URL: process.env.NBHD_API_BASE_URL,
  NBHD_TENANT_ID: process.env.NBHD_TENANT_ID,
  NBHD_INTERNAL_API_KEY: process.env.NBHD_INTERNAL_API_KEY,
};
process.env.NBHD_API_BASE_URL = API_BASE_URL;
process.env.NBHD_TENANT_ID = TENANT_ID;
process.env.NBHD_INTERNAL_API_KEY = INTERNAL_KEY;

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_CONSOLE_LOG = console.log;

after(() => {
  for (const [key, value] of Object.entries(PREVIOUS_ENV)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  globalThis.fetch = ORIGINAL_FETCH;
  console.log = ORIGINAL_CONSOLE_LOG;
});

// Belt-and-braces: even if a helper's finally block were bypassed, no test can
// leak a stubbed fetch into the next one.
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  console.log = ORIGINAL_CONSOLE_LOG;
});

// ── Plugin registry ─────────────────────────────────────────────────────────
// For each plugin: the tool whose execute() reaches the runtime-call function
// with the least ceremony, the minimal params that satisfy tool-logger's
// required-arg guard (`wrapTool` throws BEFORE execute if a `required` key is
// missing), and any pluginConfig flag the registration is gated on.
//
// `throwsOnError: false` marks the plugins whose execute() CATCHES the transport
// error and renders it into the payload instead of propagating — the message is
// still the verbatim `error.message`, so the assertions are identical.
const PLUGINS = [
  {
    dir: "nbhd-fuel-tools",
    tool: "nbhd_fuel_summary",
    params: {},
    // execute() catches and renders `{ error: error.message }`.
    throwsOnError: false,
  },
  {
    dir: "nbhd-finance-tools",
    tool: "nbhd_finance_list_accounts",
    params: {},
    throwsOnError: true,
  },
  {
    dir: "nbhd-insights-tools",
    tool: "nbhd_insights_history",
    // `pillar` is the only required key; "gravity" is the sole allowed value.
    params: { pillar: "gravity" },
    throwsOnError: true,
  },
  {
    dir: "nbhd-settings-tools",
    // Deliberately NOT nbhd_places_search — that one passes
    // allowResponseStatuses:[429,503], a different (non-throwing) path.
    tool: "nbhd_get_preferred_model_state",
    params: {},
    throwsOnError: true,
  },
  {
    dir: "nbhd-sautai-tools",
    tool: "nbhd_get_meal_plan",
    params: {},
    // execute() catches and renders `{ error: error.message }`.
    throwsOnError: false,
  },
  {
    dir: "nbhd-friends-tools",
    tool: "nbhd_mission_context",
    params: {},
    throwsOnError: true,
  },
  {
    dir: "nbhd-document-keep",
    tool: "nbhd_document_list_ingestions",
    params: {},
    // Fail-closed gate: no tools register unless this is strictly true.
    pluginConfig: { documentIngestionEnabled: true },
    throwsOnError: true,
  },
  {
    dir: "nbhd-automation-tools",
    tool: "nbhd_cron_create_pure_reminder",
    params: {
      name: "error transport probe",
      schedule: { kind: "cron", expr: "0 8 * * *", tz: "Asia/Tokyo" },
      text: "probe",
    },
    throwsOnError: true,
  },
  {
    dir: "nbhd-journal-shaping",
    tool: "nbhd_journal_template_get",
    params: {},
    // Fail-closed gate: no tools register unless this is strictly true.
    pluginConfig: { journalShapingEnabled: true },
    throwsOnError: true,
  },
  {
    dir: "nbhd-reddit-tools",
    // Uses callRedditTool (the canonical transport); nbhd_reddit_status uses a
    // separate callIntegrationsApi that returns {ok,status,data} and never throws.
    tool: "nbhd_reddit_connect",
    params: {},
    throwsOnError: true,
  },
  {
    dir: "nbhd-journal-tools",
    tool: "nbhd_document_get",
    params: { kind: "daily" },
    throwsOnError: true,
  },
  {
    dir: "nbhd-google-tools",
    tool: "nbhd_gmail_list_messages",
    params: {},
    throwsOnError: true,
  },
  {
    // The only tool this plugin registers, and a POST write — the exact shape
    // DRF answers with a validation body. It was omitted from the first rollout
    // and kept the pre-fix `parsed.error || parsed.detail` read until now.
    dir: "nbhd-agenda-tools",
    tool: "nbhd_record_commitment",
    params: {
      about: "check in on the error-transport rollout",
      surface_after: "2030-01-01T00:00:00Z",
      why: "error transport probe",
    },
    throwsOnError: true,
  },
];

assert.equal(PLUGINS.length, 13, "all 13 runtime-calling plugins must be covered");

// ── Adversarial response bodies ─────────────────────────────────────────────

// A — the validation envelope the naive `normalized.detail` read threw away.
const CASE_A_STATUS = 400;
const CASE_A_BODY = JSON.stringify({
  error: "validation_failed",
  message: "Workout validation failed",
  details: [
    {
      loc: ["exercises", 0, "sets", 0, "weighted_reps", "weight"],
      msg: "Field required",
      type: "missing",
    },
  ],
  weekday: "tuesday",
});

// B — string `detail` and nothing else: the ONE shape the old code handled.
// Its compact, brace-free rendering must be preserved exactly.
const CASE_B_STATUS = 409;
const CASE_B_BODY = JSON.stringify({
  error: "integration_refresh_failed",
  detail: "Google token refresh failed",
});

// B2 — string `detail` PLUS a sibling hint. The old code kept `detail` and
// dropped `user_action`, hiding the one instruction that fixes the problem.
const CASE_B2_STATUS = 409;
const CASE_B2_BODY = JSON.stringify({
  error: "integration_refresh_failed",
  detail: "Google token refresh failed",
  user_action: "Tell the user to reconnect Google.",
});

// C — bare DRF field errors: no `error` key at all, no `detail` key at all.
const CASE_C_STATUS = 400;
const CASE_C_BODY = JSON.stringify({ week_rating: ["This field is required."] });

// D — empty body. Must NOT produce a parenthetical, and must never leak
// "undefined" into the message the model reads.
const CASE_D_STATUS = 500;
const CASE_D_BODY = "";

// E — non-JSON body. The transports fall back to `{ raw }`; the raw text must
// survive into the message.
const CASE_E_STATUS = 502;
const CASE_E_BODY = "<html>Bad Gateway</html>";

// F — the Google provider envelope. `provider_status` used to ride along in a
// bespoke `[provider_status=403]` suffix that dropped reason and message.
const CASE_F_STATUS = 502;
const CASE_F_BODY = JSON.stringify({
  error: "provider_request_failed",
  provider_status: 403,
  provider_reason: "PERMISSION_DENIED",
  provider_message: "Request had insufficient authentication scopes.",
});

// G — an envelope big enough to blow past TOOL_ERROR_DETAIL_MAX_CHARS (2000).
// 40 entries × ~150 serialized chars each ≫ 2000.
const CASE_G_STATUS = 400;
const CASE_G_BODY = JSON.stringify({
  error: "validation_failed",
  message: "Workout validation failed",
  details: Array.from({ length: 40 }, (_, index) => ({
    loc: ["exercises", index, "sets", 0, "weighted_reps", "weight"],
    msg: `Field required — provide a numeric weight in kilograms for exercise ${index}`,
    type: "missing",
  })),
});

// H — non-ASCII + emoji. JSON.stringify must not mangle them, and nothing in
// the chain may re-encode them into \uXXXX escapes.
const CASE_H_STATUS = 400;
const CASE_H_UNICODE = "重量が必要です 🏋️";
const CASE_H_BODY = JSON.stringify({
  error: "validation_failed",
  message: CASE_H_UNICODE,
});

// The clamp is 2000 chars + the "… [truncated]" marker (13 chars). Allow a
// little slack for the marker without allowing an unclamped payload through.
const CLAMP_UPPER_BOUND = 2050;

// ── Harness ─────────────────────────────────────────────────────────────────

/**
 * tool-logger's wrapTool console.logs a line per registration and per call.
 * Across 12 plugins (nbhd-journal-tools alone registers ~50 tools) that buries
 * the test output, so we mute console.log around register/execute only. Muting
 * console.log does NOT affect node:test's reporter, which writes to
 * process.stdout directly.
 */
async function withQuietLogs(fn) {
  console.log = () => {};
  try {
    return await fn();
  } finally {
    console.log = ORIGINAL_CONSOLE_LOG;
  }
}

/** Drive the plugin's real default export with a minimal fake api. */
async function loadTools(target) {
  const module = await import(new URL(`./${target.dir}/index.js`, import.meta.url));
  const register = module.default;
  assert.equal(
    typeof register,
    "function",
    `${target.dir}: default export must be register(api)`,
  );

  const tools = new Map();
  const api = {
    pluginConfig: { apiBaseUrl: API_BASE_URL, ...(target.pluginConfig || {}) },
    // Some plugins pass (def), some (def, { optional: true }) — accept both.
    registerTool(def) {
      if (def && typeof def.name === "string") tools.set(def.name, def);
    },
    on() {},
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  };

  await withQuietLogs(async () => register(api));
  return tools;
}

/**
 * Pull the runtime error message out of whatever the tool produced.
 *
 * Plugins split two ways at the call site: most let the transport error
 * propagate, while nbhd-fuel-tools and nbhd-sautai-tools catch it and render
 * `{ error: error.message }` into `details.json`. Both carry the VERBATIM
 * message, so reading `details.json.error` first keeps even the exact-equality
 * assertion (case D) valid for the catching plugins.
 */
function extractErrorMessage(result) {
  const json = result && result.details && result.details.json;
  if (json && typeof json.error === "string") return json.error;

  const text =
    result && Array.isArray(result.content) && result.content[0]
      ? result.content[0].text
      : undefined;
  if (typeof text === "string") {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.error === "string") return parsed.error;
    } catch {
      // Not JSON — the rendered text itself is the message.
    }
    return text;
  }

  return `unrecognized tool result: ${JSON.stringify(result)}`;
}

/**
 * Register the plugin, stub fetch with the given error response, invoke the
 * target tool, and return the message the model would see.
 */
async function runCase(target, { status, body }) {
  const tools = await loadTools(target);
  const tool = tools.get(target.tool);
  assert.ok(
    tool,
    `${target.dir}: expected tool ${target.tool} to be registered (registered: ${
      [...tools.keys()].join(", ") || "<none>"
    })`,
  );

  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: false,
      status,
      statusText: "Error",
      headers: { get: () => null },
      async text() {
        return body;
      },
      async json() {
        return JSON.parse(body);
      },
    };
  };

  let message;
  let threw = false;
  try {
    message = await withQuietLogs(async () => {
      try {
        const result = await tool.execute("error-transport-test", target.params ?? {});
        return extractErrorMessage(result);
      } catch (error) {
        threw = true;
        return (error && error.message) || String(error);
      }
    });
  } finally {
    globalThis.fetch = ORIGINAL_FETCH;
  }

  // The whole suite is worthless if the tool never reached the transport —
  // a config throw or an arg-guard rejection would otherwise "pass" cases
  // that only assert `includes()`.
  assert.ok(
    calls.length >= 1,
    `${target.dir}/${target.tool}: fetch was never called — the tool failed before reaching the runtime transport (message: ${message})`,
  );
  if (target.throwsOnError) {
    assert.ok(
      threw,
      `${target.dir}/${target.tool}: expected the transport error to propagate out of execute()`,
    );
  }

  return message;
}

/** The `(...)` detail the canonical block appends, minus the wrapping parens. */
function parentheticalOf(message) {
  const open = message.indexOf(" (");
  assert.notEqual(open, -1, `expected a parenthetical detail in: ${message}`);
  const close = message.lastIndexOf(")");
  assert.ok(close > open, `expected a closing paren in: ${message}`);
  return message.slice(open + 2, close);
}

function label(target) {
  return `${target.dir} (${target.tool})`;
}

// Cases C, F, G and H are exercised against the two representative transports:
// nbhd-fuel-tools (the plugin whose DRF writes motivated the fix) and
// nbhd-google-tools (the only plugin that previously had a bespoke
// provider_status suffix).
const REPRESENTATIVE = PLUGINS.filter((p) =>
  ["nbhd-fuel-tools", "nbhd-google-tools"].includes(p.dir),
);
assert.equal(REPRESENTATIVE.length, 2, "both representative plugins must resolve");

// ── A: validation envelope survives end to end ──────────────────────────────

describe("A · validation envelope reaches the model", () => {
  for (const target of PLUGINS) {
    it(`${label(target)} preserves error, message, details[] and sibling keys`, async () => {
      const message = await runCase(target, { status: CASE_A_STATUS, body: CASE_A_BODY });

      assert.match(message, /NBHD runtime error 400:/, message);
      // The error code still leads the message.
      assert.ok(message.includes("validation_failed"), message);
      // The details[] payload the naive `normalized.detail` read discarded.
      assert.ok(message.includes("weight"), message);
      assert.ok(message.includes("missing"), message);
      // An arbitrary sibling key — proof this isn't a hard-coded allowlist.
      assert.ok(message.includes("weekday"), message);
    });
  }
});

// ── B: string-detail-only keeps its old compact shape ───────────────────────

describe("B · string-detail-only responses keep the old compact form", () => {
  for (const target of PLUGINS) {
    it(`${label(target)} renders the bare detail string with no JSON braces`, async () => {
      const message = await runCase(target, { status: CASE_B_STATUS, body: CASE_B_BODY });

      assert.ok(message.includes("integration_refresh_failed"), message);
      assert.ok(message.includes("Google token refresh failed"), message);
      // No `{` anywhere: a single string `detail` must NOT be JSON-serialized.
      assert.ok(
        !message.includes("{"),
        `expected no JSON serialization for a string-only detail: ${message}`,
      );
      assert.equal(parentheticalOf(message), "Google token refresh failed", message);
    });
  }
});

// ── B2: sibling keys alongside a string detail are no longer dropped ────────

describe("B2 · a string detail with sibling keys keeps both", () => {
  for (const target of PLUGINS) {
    it(`${label(target)} keeps detail AND the user_action hint`, async () => {
      const message = await runCase(target, { status: CASE_B2_STATUS, body: CASE_B2_BODY });

      assert.ok(message.includes("Google token refresh failed"), message);
      // The actionable hint the old code silently discarded.
      assert.ok(message.includes("reconnect Google"), message);
    });
  }
});

// ── C: DRF top-level field errors with no `error` key ───────────────────────

describe("C · bare DRF field errors (no error key)", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} falls back to runtime_request_failed and names the field`, async () => {
      const message = await runCase(target, { status: CASE_C_STATUS, body: CASE_C_BODY });

      assert.ok(message.includes("runtime_request_failed"), message);
      assert.ok(message.includes("week_rating"), message);
      assert.ok(message.includes("This field is required."), message);
    });
  }
});

// ── D: empty body produces no parenthetical and no "undefined" ──────────────

describe("D · empty body", () => {
  for (const target of PLUGINS) {
    it(`${label(target)} emits exactly the bare status message`, async () => {
      const message = await runCase(target, { status: CASE_D_STATUS, body: CASE_D_BODY });

      assert.equal(message, "NBHD runtime error 500: runtime_request_failed");
      assert.ok(!message.includes("undefined"), message);
      assert.ok(!message.includes("("), message);
    });
  }
});

// ── E: non-JSON body survives through the raw fallback ──────────────────────

describe("E · non-JSON body", () => {
  for (const target of PLUGINS) {
    it(`${label(target)} surfaces the raw upstream text`, async () => {
      const message = await runCase(target, { status: CASE_E_STATUS, body: CASE_E_BODY });

      assert.ok(message.includes("NBHD runtime error 502"), message);
      assert.ok(message.includes("runtime_request_failed"), message);
      assert.ok(message.includes("Bad Gateway"), message);
    });
  }
});

// ── F: the provider envelope keeps status, reason AND message ───────────────

describe("F · provider envelope", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} keeps provider_status, provider_reason and provider_message`, async () => {
      const message = await runCase(target, { status: CASE_F_STATUS, body: CASE_F_BODY });

      assert.ok(message.includes("provider_request_failed"), message);
      assert.ok(message.includes("403"), message);
      assert.ok(message.includes("PERMISSION_DENIED"), message);
      assert.ok(message.includes("insufficient authentication scopes"), message);
    });
  }
});

// ── G: oversized payloads are clamped, not dumped ───────────────────────────

describe("G · clamp", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} truncates a payload past TOOL_ERROR_DETAIL_MAX_CHARS`, async () => {
      assert.ok(
        CASE_G_BODY.length > 2000,
        "the clamp fixture must exceed 2000 serialized chars",
      );
      const message = await runCase(target, { status: CASE_G_STATUS, body: CASE_G_BODY });

      assert.ok(message.includes("validation_failed"), message);
      assert.ok(
        message.includes("… [truncated]"),
        `expected the truncation marker in: ${message.slice(0, 200)}…`,
      );

      const detail = parentheticalOf(message);
      assert.ok(
        detail.length <= CLAMP_UPPER_BOUND,
        `detail should be clamped to ~2000 chars, got ${detail.length}`,
      );
      // ...and it must still be long enough to be useful, not clipped to nothing.
      assert.ok(detail.length > 1000, `detail unexpectedly short: ${detail.length}`);
    });
  }
});

// ── Adversarial extension harness ───────────────────────────────────────────

/**
 * Like `runCase`, but the caller supplies the whole fake Response (or a factory
 * that throws), and both the message AND whether execute() threw are returned.
 * Needed for the cases below that probe transport robustness rather than the
 * shape of a well-formed error body.
 */
async function runWithResponse(target, makeResponse) {
  const tools = await loadTools(target);
  const tool = tools.get(target.tool);
  assert.ok(tool, `${target.dir}: expected tool ${target.tool} to be registered`);

  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return makeResponse();
  };

  let message;
  let threw = false;
  try {
    message = await withQuietLogs(async () => {
      try {
        const result = await tool.execute("error-transport-test", target.params ?? {});
        return extractErrorMessage(result);
      } catch (error) {
        threw = true;
        return (error && error.message) || String(error);
      }
    });
  } finally {
    globalThis.fetch = ORIGINAL_FETCH;
  }

  assert.ok(calls.length >= 1, `${target.dir}/${target.tool}: fetch was never called`);
  return { message, threw };
}

/** A well-formed error Response whose body is `body`. */
function errorResponse(status, body) {
  return {
    ok: false,
    status,
    statusText: "Error",
    headers: { get: () => null },
    async text() {
      return body;
    },
    async json() {
      return JSON.parse(body);
    },
  };
}

// ── I: falsy-but-PRESENT detail values are not silently dropped ─────────────
// The old `asTrimmedString(normalized.detail)` returned "" for every non-string,
// so `detail: 0` and `detail: false` vanished. Under the contract they take the
// JSON.stringify branch (typeof detail !== "string") and survive as "0"/"false".
// `null`, `{}` and `[]` must still collapse to no parenthetical at all.

describe("I · falsy-but-present detail values", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} preserves detail:0 and detail:false, drops null/{}/[]`, async () => {
      const zero = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_value", detail: 0 }),
      });
      assert.equal(zero, "NBHD runtime error 400: bad_value (0)", zero);

      const bool = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_value", detail: false }),
      });
      assert.equal(bool, "NBHD runtime error 400: bad_value (false)", bool);

      // null / empty object / empty array carry no information — no empty "()".
      for (const empty of [null, {}, []]) {
        const message = await runCase(target, {
          status: 400,
          body: JSON.stringify({ error: "bad_value", detail: empty }),
        });
        assert.equal(message, "NBHD runtime error 400: bad_value", message);
        assert.ok(!message.includes("("), message);
        assert.ok(!message.includes("null"), message);
      }
    });
  }
});

// ── J: a non-string `detail` is serialized, not discarded ───────────────────
// A nested validation object under `detail` is the single most common DRF shape
// after the top-level one, and it is exactly what the naive read threw away.

describe("J · non-string detail is serialized", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} serializes a nested detail object and a detail array`, async () => {
      const nested = await runCase(target, {
        status: 400,
        body: JSON.stringify({ detail: { week_rating: ["This field is required."] } }),
      });
      assert.ok(nested.includes("runtime_request_failed"), nested);
      assert.equal(
        parentheticalOf(nested),
        '{"week_rating":["This field is required."]}',
        nested,
      );

      const arrayDetail = await runCase(target, {
        status: 422,
        body: JSON.stringify({ error: "validation_failed", detail: ["first", "second"] }),
      });
      assert.equal(parentheticalOf(arrayDetail), '["first","second"]', arrayDetail);
    });
  }
});

// ── K: the STRING-detail clamp branch ───────────────────────────────────────
// Case G only exercises the JSON.stringify clamp. The `detailIsOnlyKey &&
// typeof detail === "string"` branch has its own clampErrorDetail() call, and an
// upstream stack trace dumped into `detail` is precisely how it gets hit.

describe("K · clamp on the string-detail branch", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} truncates an oversized string detail without JSON-quoting it`, async () => {
      const huge = "E".repeat(5000);
      const message = await runCase(target, {
        status: 500,
        body: JSON.stringify({ error: "upstream_exploded", detail: huge }),
      });

      const detail = parentheticalOf(message);
      assert.ok(detail.includes("… [truncated]"), detail.slice(0, 80));
      assert.ok(
        detail.length <= CLAMP_UPPER_BOUND,
        `expected clamp to ~2000 chars, got ${detail.length}`,
      );
      // The string branch must NOT run through JSON.stringify — no wrapping quotes.
      assert.ok(!detail.startsWith('"'), `string detail was JSON-quoted: ${detail.slice(0, 40)}`);
      assert.ok(detail.startsWith("EEEE"), detail.slice(0, 40));
    });
  }
});

// ── L: payloads that must yield NO parenthetical ────────────────────────────

describe("L · payloads carrying no usable detail", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} emits a bare message for error-only and whitespace-only detail`, async () => {
      // Only an `error` key: the filter strips it, entries is empty.
      const errorOnly = await runCase(target, {
        status: 403,
        body: JSON.stringify({ error: "forbidden" }),
      });
      assert.equal(errorOnly, "NBHD runtime error 403: forbidden", errorOnly);

      // A whitespace-only string detail trims to "" — no empty "( )".
      const blank = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_request", detail: "   \n\t " }),
      });
      assert.equal(blank, "NBHD runtime error 400: bad_request", blank);
      assert.ok(!blank.includes("("), blank);

      // `{}` — no error key, no detail key.
      const emptyObject = await runCase(target, { status: 500, body: "{}" });
      assert.equal(emptyObject, "NBHD runtime error 500: runtime_request_failed", emptyObject);
    });
  }
});

// ── M: transport robustness ─────────────────────────────────────────────────
// These do not probe compactErrorDetail — they probe that a malformed Response
// still fails LOUDLY instead of being reported to the model as a success.

describe("M · malformed Response objects still fail loudly", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} surfaces an error when response.text is missing`, async () => {
      const { message } = await runWithResponse(target, () => ({
        ok: false,
        status: 500,
        statusText: "Error",
        headers: { get: () => null },
        // no text(), no json()
      }));
      assert.ok(
        /text is not a function/.test(message),
        `expected a loud failure, got: ${message}`,
      );
      // Crucially: it must NOT look like a successful/empty runtime answer.
      assert.ok(!/^NBHD runtime error 500: runtime_request_failed$/.test(message), message);
    });

    it(`${label(target)} propagates a rejecting response.text()`, async () => {
      const { message } = await runWithResponse(target, () => ({
        ok: false,
        status: 502,
        statusText: "Error",
        headers: { get: () => null },
        async text() {
          throw new Error("socket hang up");
        },
      }));
      assert.ok(message.includes("socket hang up"), message);
    });
  }
});

// ── N: a top-level JSON ARRAY body is dropped by asObject (characterization) ─
// DRF can answer a list serializer with a bare array, e.g. ["This field is
// required."]. `asObject` returns {} for arrays, so the contract discards it and
// the model sees only the status. This test PINS that contract behavior so the
// information loss is visible and any future change to it is deliberate.

describe("N · top-level array bodies (contract characterization)", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} reduces a bare array body to the status-only message`, async () => {
      const message = await runCase(target, {
        status: 400,
        body: JSON.stringify(["This field is required."]),
      });
      assert.equal(message, "NBHD runtime error 400: runtime_request_failed", message);
      // Documented gap: the array contents do NOT reach the model.
      assert.ok(!message.includes("This field is required."), message);
    });
  }
});

// ── O: the two catching plugins really do catch ─────────────────────────────
// `throwsOnError: false` only SKIPS the propagation assert, so a fuel/sautai
// regression that started throwing would slip through every case above. Pin the
// documented split in both directions.

describe("O · the documented throw/catch split holds", () => {
  for (const target of PLUGINS.filter((p) => p.throwsOnError === false)) {
    it(`${label(target)} renders the transport error instead of throwing`, async () => {
      const { message, threw } = await runWithResponse(target, () =>
        errorResponse(400, CASE_A_BODY),
      );
      assert.equal(threw, false, `${target.dir} unexpectedly threw out of execute()`);
      assert.ok(message.includes("validation_failed"), message);
      assert.ok(message.includes("weekday"), message);
    });
  }
});

// ── H: unicode and emoji round-trip unmangled ───────────────────────────────

describe("H · unicode", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} passes non-ASCII and emoji through verbatim`, async () => {
      const message = await runCase(target, { status: CASE_H_STATUS, body: CASE_H_BODY });

      assert.ok(
        message.includes(CASE_H_UNICODE),
        `expected the verbatim unicode string in: ${message}`,
      );
      // No \uXXXX escaping anywhere in the chain.
      assert.ok(!message.includes("\\u"), message);
    });
  }
});

// ── P: a NESTED `error` object is filtered away entirely (characterization) ──
// `compactErrorDetail` drops the `error` key unconditionally, and
// `asTrimmedString` yields "" for a non-string. So a body whose ONLY key is a
// nested `error` object — the raw Google/OAuth API shape,
// `{"error":{"code":403,"message":"...","status":"PERMISSION_DENIED"}}` —
// reaches the model as a bare status line with every word of the upstream
// explanation removed.
//
// This is contract behavior, not a plugin bug: the runtime views normalize that
// shape server-side (`_provider_error_response` lifts `provider_reason` /
// `provider_message` out of it), so plugins normally receive the flat envelope
// case F covers. Pinned here so the blind spot is visible and any future change
// to it is deliberate rather than accidental.

describe("P · nested error objects (contract characterization)", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} drops a nested error object but keeps its siblings`, async () => {
      const nestedOnly = await runCase(target, {
        status: 403,
        body: JSON.stringify({
          error: {
            code: 403,
            message: "Insufficient Permission",
            status: "PERMISSION_DENIED",
          },
        }),
      });
      // Documented gap: nothing from the nested object survives.
      assert.equal(nestedOnly, "NBHD runtime error 403: runtime_request_failed", nestedOnly);
      assert.ok(!nestedOnly.includes("PERMISSION_DENIED"), nestedOnly);
      assert.ok(!nestedOnly.includes("Insufficient Permission"), nestedOnly);
      // It must at least not degrade into "[object Object]".
      assert.ok(!nestedOnly.includes("[object"), nestedOnly);

      // A sibling key alongside the nested error still survives, which is what
      // keeps the gap narrow: only the `error` key itself is sacrificed.
      const withSibling = await runCase(target, {
        status: 403,
        body: JSON.stringify({
          error: { code: 403, message: "Insufficient Permission" },
          user_action: "Tell the user to reconnect Google.",
        }),
      });
      assert.ok(withSibling.includes("reconnect Google"), withSibling);
      assert.ok(!withSibling.includes("Insufficient Permission"), withSibling);
    });
  }
});

// ── Q: the clamp boundary is exact ──────────────────────────────────────────
// Cases G and K only prove that something MUCH larger than 2000 gets truncated.
// The comparison is `<=`, so a 2000-char detail must pass through untouched and
// a 2001-char one must be cut to exactly 2000 chars plus the marker. An
// off-by-one here would either corrupt a payload that fits or let one through.

describe("Q · clamp boundary is exact at TOOL_ERROR_DETAIL_MAX_CHARS", () => {
  const MARKER = "… [truncated]";

  for (const target of REPRESENTATIVE) {
    it(`${label(target)} passes 2000 chars intact and truncates 2001`, async () => {
      const exact = "A".repeat(2000);
      const atLimit = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_request", detail: exact }),
      });
      const atLimitDetail = parentheticalOf(atLimit);
      assert.equal(atLimitDetail.length, 2000, `expected no truncation at exactly 2000`);
      assert.equal(atLimitDetail, exact);
      assert.ok(!atLimitDetail.includes(MARKER), "2000 chars must not be marked truncated");

      const over = "B".repeat(2001);
      const past = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_request", detail: over }),
      });
      const pastDetail = parentheticalOf(past);
      assert.ok(pastDetail.endsWith(MARKER), pastDetail.slice(-40));
      // Exactly 2000 retained chars + the marker — no more, no less.
      assert.equal(pastDetail.slice(0, -MARKER.length), "B".repeat(2000));
      assert.equal(pastDetail.length, 2000 + MARKER.length);
    });
  }
});

// ── R: the one divergent call site (allowResponseStatuses) still behaves ────
// nbhd-settings-tools is the only plugin whose guard is
// `!response.ok && !allowResponseStatuses.includes(response.status)`, and the
// drift guard deliberately does NOT pin that condition. Nothing else in this
// suite drives nbhd_places_search, so a bad edit that dropped the allowlist —
// turning a soft 429 into a hard throw — would go unnoticed. Both directions
// are pinned: an allow-listed status must NOT raise, a non-allow-listed one must.

describe("R · nbhd-settings-tools allowResponseStatuses path", () => {
  const placesTarget = {
    dir: "nbhd-settings-tools",
    tool: "nbhd_places_search",
    params: { query: "coffee" },
    // Fail-closed gate: nbhd_places_search (and nbhd_tour_guide) only register
    // when the version-gated Django config turns tour-guide delivery on.
    pluginConfig: { tourGuideEnabled: true },
  };

  it("does not throw a runtime error for an allow-listed 429", async () => {
    const { message, threw } = await runWithResponse(placesTarget, () =>
      errorResponse(429, JSON.stringify({ verified: false, places: [] })),
    );
    assert.equal(threw, false, `an allow-listed 429 must not propagate: ${message}`);
    assert.ok(
      !/NBHD runtime error 429/.test(message),
      `the allowlist was bypassed: ${message}`,
    );
  });

  it("still throws — with the full detail — for a non-allow-listed 400", async () => {
    const { message, threw } = await runWithResponse(placesTarget, () =>
      errorResponse(400, CASE_C_BODY),
    );
    assert.equal(threw, true, `a 400 must still propagate: ${message}`);
    assert.ok(message.includes("NBHD runtime error 400"), message);
    // The whole point of the branch: the DRF field errors still reach the model.
    assert.ok(message.includes("week_rating"), message);
    assert.ok(message.includes("This field is required."), message);
  });
});

// ── S: a rejecting fetch surfaces loudly ────────────────────────────────────
// Case M probes a malformed Response; this probes the layer below it — the
// network call itself failing (DNS, refused connection, TLS). The failure must
// reach the model as an error, never as an empty-but-successful runtime answer,
// and must not be misreported as the timeout message (the catch block special-
// cases `AbortError`, and a plain rejection must not fall into that branch).

describe("S · transport-level fetch rejection", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} surfaces a refused connection instead of a silent empty result`, async () => {
      const { message } = await runWithResponse(target, () => {
        throw new Error("connect ECONNREFUSED 127.0.0.1:443");
      });

      assert.ok(message.includes("ECONNREFUSED"), `expected the network error: ${message}`);
      assert.ok(
        !/^NBHD runtime error \d+: runtime_request_failed$/.test(message),
        `a network failure was laundered into a runtime envelope: ${message}`,
      );
      assert.ok(!/timed out/.test(message), `misreported as a timeout: ${message}`);
    });
  }
});

// ══════════════════════════════════════════════════════════════════════════════
// ADVERSARIAL EXTENSION (verification round 2)
//
// Cases T–W probe edges the original suite left open. Each was first checked
// against a standalone reimplementation of the authoritative contract, so the
// expectations below are what the CONTRACT dictates — not what the current code
// happens to do. Where the contract loses information, the test pins the loss
// (characterization) so a future change to it has to be deliberate.
// ══════════════════════════════════════════════════════════════════════════════

// ── T: `error` is present but not a usable string ───────────────────────────
// `asTrimmedString` yields "" for a non-string AND for a whitespace-only string,
// so both must fall back to the `runtime_request_failed` code. Critically, the
// entries filter drops the "error" KEY regardless of its value — so a body whose
// only other key is `detail` must still take the string-detail branch and
// deliver the reason. A regression that filtered on the error VALUE instead of
// the key would silently JSON-blob the whole body here.

describe("T · unusable `error` values fall back to the default code", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} falls back for an object error and a blank error, keeping detail`, async () => {
      const objectError = await runCase(target, {
        status: 403,
        body: JSON.stringify({ error: { code: 5, message: "nope" }, detail: "boom" }),
      });
      assert.equal(
        objectError,
        "NBHD runtime error 403: runtime_request_failed (boom)",
        objectError,
      );

      const blankError = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "   ", detail: "real reason" }),
      });
      assert.equal(
        blankError,
        "NBHD runtime error 400: runtime_request_failed (real reason)",
        blankError,
      );
    });
  }
});

// ── U: non-object JSON bodies collapse to status-only (characterization) ────
// Case N pinned the top-level ARRAY. `asObject` also returns {} for the three
// other JSON top-level forms — `null`, a bare string, a bare number — each of
// which a misbehaving upstream or a proxy error page can produce. All must
// degrade to the status-only message rather than crashing or, worse, rendering
// "null"/"undefined" into the model-facing text.

describe("U · non-object JSON bodies (contract characterization)", () => {
  const BODIES = [
    ["JSON null", "null"],
    ["bare string", JSON.stringify("gateway exploded")],
    ["bare number", "42"],
  ];

  for (const target of REPRESENTATIVE) {
    for (const [name, body] of BODIES) {
      it(`${label(target)} reduces a ${name} body to the status-only message`, async () => {
        const message = await runCase(target, { status: 502, body });
        assert.equal(message, "NBHD runtime error 502: runtime_request_failed", message);
        // No empty parenthetical, and no stringified junk leaking through.
        assert.ok(!message.includes("("), message);
        assert.ok(!/null|undefined|NaN/.test(message), message);
      });
    }
  }
});

// ── V: the detail:null asymmetry is real and must stay visible ──────────────
// `{error, detail:null}` drops the detail (the `value === null` guard), but
// `{error, detail:null, field}` keeps it — the sibling flips detailIsOnlyKey to
// false, so the whole entry map is serialized INCLUDING the null. Same for an
// empty-string detail. This is a genuine inconsistency in the contract; pin it
// so nobody "fixes" one side without noticing the other.

describe("V · detail:null is dropped alone but retained beside a sibling", () => {
  for (const target of REPRESENTATIVE) {
    it(`${label(target)} drops a lone null detail and keeps it when a sibling exists`, async () => {
      const alone = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_value", detail: null }),
      });
      assert.equal(alone, "NBHD runtime error 400: bad_value", alone);

      const withSibling = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_value", detail: null, field: "z" }),
      });
      assert.equal(
        parentheticalOf(withSibling),
        '{"detail":null,"field":"z"}',
        withSibling,
      );

      // Same shape for an empty-string detail: dropped alone, kept beside a sibling.
      const blankAlone = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_value", detail: "   " }),
      });
      assert.equal(blankAlone, "NBHD runtime error 400: bad_value", blankAlone);

      const blankWithSibling = await runCase(target, {
        status: 400,
        body: JSON.stringify({ error: "bad_value", detail: "", field: "z" }),
      });
      assert.equal(
        parentheticalOf(blankWithSibling),
        '{"detail":"","field":"z"}',
        blankWithSibling,
      );
    });
  }
});

// ── W: the clamp splits astral characters (contract characterization) ───────
// `clampErrorDetail` slices at 2000 UTF-16 CODE UNITS, not code points. An
// emoji or other astral character straddling index 2000 is cut in half, leaving
// a LONE HIGH SURROGATE at the end of the model-facing message — it renders as
// U+FFFD. Cosmetic, but it is real, and this pins it: if the clamp is ever made
// surrogate-aware, this test flips and should be updated deliberately.

describe("W · clamp boundary splits a straddling surrogate pair", () => {
  const LONE_HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/;

  for (const target of REPRESENTATIVE) {
    it(`${label(target)} emits a lone surrogate when an emoji straddles index 2000`, async () => {
      // 1999 filler chars, then a 2-code-unit astral char starting AT index 1999
      // so the slice at 2000 lands mid-pair.
      const straddling = `${"a".repeat(1999)}🏋${"b".repeat(50)}`;
      const message = await runCase(target, {
        status: 500,
        body: JSON.stringify({ error: "upstream_exploded", detail: straddling }),
      });

      const detail = parentheticalOf(message);
      assert.ok(detail.includes("… [truncated]"), detail.slice(-40));
      assert.ok(
        LONE_HIGH_SURROGATE.test(detail),
        "expected the documented mid-pair split; if the clamp became surrogate-aware, update this test",
      );

      // Regardless of the split, the clamp must still bound the length.
      assert.ok(detail.length <= CLAMP_UPPER_BOUND, `detail length ${detail.length}`);

      // Sanity: a pair that does NOT straddle the boundary stays intact.
      const safe = `${"a".repeat(1990)}🏋${"b".repeat(50)}`;
      const safeMessage = await runCase(target, {
        status: 500,
        body: JSON.stringify({ error: "upstream_exploded", detail: safe }),
      });
      const safeDetail = parentheticalOf(safeMessage);
      assert.ok(safeDetail.includes("🏋"), "an emoji well inside the clamp must survive intact");
      assert.ok(!LONE_HIGH_SURROGATE.test(safeDetail), safeDetail.slice(-40));
    });
  }
});
