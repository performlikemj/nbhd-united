/**
 * NBHD Cron Enforcement Plugin
 *
 * Fire-time OUTBOUND enforcement for typed cron patterns. Zero Django calls —
 * the contract travels with the job, baked at create/save time by
 * apps/cron/signals.py into ``CronJob.data["description"]`` as
 * ``"nbhd.v1 " + JSON``. Two hooks:
 *
 *   1. cron_changed(action="started")
 *        Parse this cron's baked contract off ``event.job.description``.
 *        Cache {contract, cronName, revisions:0} under BOTH sessionKey and
 *        runId (whichever the firing hook happens to carry). No contract
 *        (freeform/legacy/no-pattern cron, or malformed description) → no
 *        cache entry, fail-open.
 *
 *   2. before_tool_call
 *        THE chokepoint. Typed crons build with ``delivery:{"mode":"none"}``
 *        and deliver by the agent calling the ``nbhd_send_to_user`` TOOL —
 *        message_sending never sees a mode:"none" cron's content (that hook
 *        only fires inside OC's own channel dispatch pipeline), so the tool
 *        call IS the delivery. Evaluate the outgoing message against the
 *        cached contract's ``check``; on failure, apply ``on_fail``:
 *        rewrite (swap in safe content), revise (block + ask the model to
 *        try again, bounded by max_revisions), or allow (ship as-is once the
 *        revision budget is exhausted and there's no safe rewrite).
 *
 *   3. cron_changed(action="finished" | "removed")
 *        Drop the cache entry (both keys).
 *
 * FAIL-OPEN DISCIPLINE: ``before_tool_call`` is FAIL-CLOSED at the runtime
 * level (a throw blocks the tool call) — for EVERY tenant, once this plugin
 * is enabled, not just typed-cron sessions. So the entire hook body is
 * try/caught, returning undefined on any error, and the very first checks
 * are an O(1) cache lookup + miss-return, before any dispatch-shape parsing
 * or contract evaluation. Same discipline as nbhd-routing-context's
 * before_tool_call guard.
 *
 * Handles both dispatch shapes: a direct call (toolName === "nbhd_send_to_user",
 * params.message) and the toolSearch meta-dispatch (toolName === "tool_call",
 * params.id === "nbhd_send_to_user", with the real tool's own arguments
 * nested under params.params or params.arguments) — mirrors
 * extractDispatchedToolId from nbhd-routing-context/index.js.
 *
 * Hook contract verified against `dist/plugin-sdk/src/plugins/hook-types.d.ts`
 * in openclaw@2026.5.28. Registered via `api.on` (5.28-correct) —
 * precedent: nbhd-routing-context, nbhd-activity-stream.
 *
 * The toolsAllow restriction baked into each pattern's OC payload at create
 * time is the structural mutation guard (cron-turn agents literally cannot
 * call nbhd_task_create etc.). This plugin is the outbound content guard on
 * top of that.
 */

const DEFAULT_CACHE_TTL_SECONDS = 600;
const CONTRACT_PREFIX = "nbhd.v1 ";
const SEND_TOOL_ID = "nbhd_send_to_user";
const TOOL_DISPATCH_META = "tool_call";

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") return defaultValue;
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) return defaultValue;
  return Math.max(min, Math.min(max, parsed));
}

function safeLog(api, level, message) {
  try {
    if (api && api.logger && typeof api.logger[level] === "function") {
      api.logger[level](message);
    }
  } catch (_ignored) {
    // Logging must never affect the enforcement decision or throw past the
    // caller — a broken logger is not grounds to block or lose a decision.
  }
}

// Like safeLog, but for logging a caught `error` of unknown shape. Every step
// that touches the error value — reading `.message` (which could be a
// throwing getter) and stringifying it into the log line (a thrown Symbol
// throws on implicit ToString) — happens INSIDE this function's own
// try/catch, so a poisoned error value can never escape the catch block that
// called us and propagate out of a fail-open hook.
function safeLogError(api, level, prefix, error) {
  try {
    let detail;
    try {
      detail = (error && error.message) || error;
    } catch (_ignored2) {
      detail = "unrecoverable error detail";
    }
    const message = `${prefix}: ${detail}`;
    if (api && api.logger && typeof api.logger[level] === "function") {
      api.logger[level](message);
    }
  } catch (_ignored) {
    // Same guarantee as safeLog — a broken logger or a poisoned error value
    // (Symbol, throwing message getter/toString) is not grounds to block or
    // lose the caller's decision.
  }
}

function getPluginConfig(api) {
  const pluginConfig = asObject(api && api.pluginConfig);
  const cacheTtlMs =
    parseInteger(pluginConfig.cacheTtlSeconds, {
      defaultValue: DEFAULT_CACHE_TTL_SECONDS,
      min: 60,
      max: 1800,
    }) * 1000;
  return { cacheTtlMs };
}

// ── contract parsing ────────────────────────────────────────────────────────
// Envelope-only validation: is this JSON at all, with the right prefix and
// version. Semantic validation of check/on_fail is pushed to evaluateCheck /
// decideGuardAction (single source of truth), which handle absence/garbage
// defensively (fail-open) rather than duplicating shape checks here.
export function parseContract(description) {
  const raw = asTrimmedString(description);
  if (!raw.startsWith(CONTRACT_PREFIX)) return null;
  try {
    const parsed = JSON.parse(raw.slice(CONTRACT_PREFIX.length));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    if (parsed.v !== 1) return null;
    return parsed;
  } catch {
    return null;
  }
}

// ── check evaluation ────────────────────────────────────────────────────────
// Mirrors apps/cron/patterns/*.py::validate_outbound_message exactly (see the
// parity test in apps/cron/tests/test_patterns.py::PARITY_CASES and this
// file's own PARITY_CASES). "contains"/"bounded" compare against TRIMMED
// content (Python: `(content or "").strip()`). "marker" is written against
// RAW content (Python: `marker in (content or "")`) but at the actual
// before_tool_call call site the message has already been trimmed by
// extractSendMessage before evaluateCheck ever sees it — behaviorally
// equivalent here, since trimming only strips leading/trailing whitespace
// and can't remove a non-whitespace marker substring from the middle.
//
// "bounded" counts Unicode CODE POINTS (`[...trimmed].length`), matching
// Python's `len(str)` (also code-point-based), NOT UTF-16 code units
// (`.length`) — an astral character (e.g. an emoji outside the BMP) is one
// code point but two UTF-16 units, so `.length` alone would overcount
// relative to Python and false-positive-reject content Python accepts.
export function evaluateCheck(check, content) {
  const text = typeof content === "string" ? content : "";
  const trimmed = text.trim();
  if (!check || typeof check !== "object") return true; // no/garbage check → nothing to enforce

  const kind = asTrimmedString(check.kind);
  if (kind === "contains") {
    const expected = asTrimmedString(check.text);
    if (!expected) return true; // vacuous — mirrors the Python `if not expected: return True`
    return trimmed.includes(expected);
  }
  if (kind === "marker") {
    const marker = asTrimmedString(check.marker);
    if (!marker) return true;
    return text.includes(marker);
  }
  if (kind === "bounded") {
    const max = typeof check.max === "number" && Number.isFinite(check.max) ? check.max : Infinity;
    const codePointLength = [...trimmed].length;
    return codePointLength > 0 && codePointLength <= max;
  }
  return true; // unknown kind → fail-open, never block on a contract we don't understand
}

function buildReviseReason(check) {
  const kind = check && asTrimmedString(check.kind);
  if (kind === "contains") {
    const expected = asTrimmedString(check.text);
    return (
      `Your message to nbhd_send_to_user did not include the required verbatim text ` +
      `${JSON.stringify(expected)}. Revise your message so it includes that text exactly, ` +
      `then call \`nbhd_send_to_user\` again.`
    );
  }
  if (kind === "marker") {
    const marker = asTrimmedString(check.marker);
    return (
      `Your message to nbhd_send_to_user is missing the required marker ${JSON.stringify(marker)}. ` +
      `Add it (on the first line), then call \`nbhd_send_to_user\` again.`
    );
  }
  return "Your message didn't satisfy this scheduled task's contract. Revise and try again.";
}

// Pure decision function: given the cached contract, the outgoing message
// content, and how many revisions this session has already used, decide what
// to do. Never mutates anything — the caller (before_tool_call) owns the
// revision counter and cache. Exported for unit tests.
export function decideGuardAction(contract, content, revisionsUsed) {
  const onFail = contract && contract.on_fail;
  if (!onFail || typeof onFail !== "object") return { type: "pass" };

  const check = contract && contract.check;
  if (evaluateCheck(check, content)) return { type: "pass" };

  const action = asTrimmedString(onFail.action);
  const maxRevisions =
    typeof onFail.max_revisions === "number" && Number.isFinite(onFail.max_revisions) ? onFail.max_revisions : 0;
  const usedSoFar = typeof revisionsUsed === "number" && Number.isFinite(revisionsUsed) ? revisionsUsed : 0;

  if (action === "rewrite") {
    return { type: "rewrite", content: asTrimmedString(onFail.content) };
  }
  if (action === "revise_then_rewrite") {
    if (usedSoFar < maxRevisions) return { type: "revise", reason: buildReviseReason(check) };
    return { type: "rewrite", content: asTrimmedString(onFail.content) };
  }
  if (action === "revise_then_allow") {
    if (usedSoFar < maxRevisions) return { type: "revise", reason: buildReviseReason(check) };
    return { type: "allow" };
  }
  return { type: "pass" }; // unknown action → fail-open
}

// ── dispatch-shape handling ─────────────────────────────────────────────────
// Mirrors extractDispatchedToolId from nbhd-routing-context/index.js:69-73 —
// the toolSearch meta-tool dispatch wraps the real tool id under id/toolId/
// tool/name.
function extractDispatchedToolId(params) {
  if (!params || typeof params !== "object") return "";
  const raw = params.id ?? params.toolId ?? params.tool ?? params.name ?? "";
  return typeof raw === "string" ? raw.trim().toLowerCase() : "";
}

// Resolve the outgoing nbhd_send_to_user message from EITHER dispatch shape.
// Returns {matched:false} for anything that isn't a dispatch of that tool —
// the caller no-ops on that. Exported for unit tests.
export function extractSendMessage(event) {
  const toolName = asTrimmedString(event && event.toolName);
  const params = asObject(event && event.params);

  if (toolName === SEND_TOOL_ID) {
    return { matched: true, shape: "direct", nestedKey: null, message: asTrimmedString(params.message) };
  }
  if (toolName === TOOL_DISPATCH_META) {
    if (extractDispatchedToolId(params) !== SEND_TOOL_ID) return { matched: false };
    if (params.params && typeof params.params === "object" && !Array.isArray(params.params)) {
      return { matched: true, shape: "meta", nestedKey: "params", message: asTrimmedString(params.params.message) };
    }
    if (params.arguments && typeof params.arguments === "object" && !Array.isArray(params.arguments)) {
      return {
        matched: true,
        shape: "meta",
        nestedKey: "arguments",
        message: asTrimmedString(params.arguments.message),
      };
    }
    // Dispatched to nbhd_send_to_user but no recognizable nested-args shape —
    // nothing we can validate or rewrite; let the call proceed untouched.
    return { matched: false };
  }
  return { matched: false };
}

// Build a replacement `params` object for a rewrite decision, preserving the
// dispatch's nested shape (direct vs toolSearch meta under params/arguments)
// so only `message` changes and every other field (job_name, id, ...) survives.
function buildRewriteParams(event, dispatch, newMessage) {
  const originalParams = asObject(event && event.params);
  if (dispatch.shape === "direct") {
    return { ...originalParams, message: newMessage };
  }
  const key = dispatch.nestedKey;
  const inner = asObject(originalParams[key]);
  return { ...originalParams, [key]: { ...inner, message: newMessage } };
}

// ── per-session cache ────────────────────────────────────────────────────────
// Cached under BOTH sessionKey and runId (whichever a given hook invocation
// carries) so a before_tool_call event that only has one of the two still
// finds the entry cron_changed seeded under the other. Both keys point at the
// SAME entry object, so mutating entry.revisions is visible under either key.
const runContextCache = new Map();

function setCacheEntry(entry, sessionKey, runId) {
  const sKey = asTrimmedString(sessionKey);
  const rKey = asTrimmedString(runId);
  if (sKey) runContextCache.set(sKey, entry);
  if (rKey && rKey !== sKey) runContextCache.set(rKey, entry);
}

function deleteCacheEntry(sessionKey, runId) {
  const sKey = asTrimmedString(sessionKey);
  const rKey = asTrimmedString(runId);
  if (sKey) runContextCache.delete(sKey);
  if (rKey) runContextCache.delete(rKey);
}

function lookupCacheEntry(sessionKey, runId) {
  const sKey = asTrimmedString(sessionKey);
  if (sKey && runContextCache.has(sKey)) return runContextCache.get(sKey);
  const rKey = asTrimmedString(runId);
  if (rKey && runContextCache.has(rKey)) return runContextCache.get(rKey);
  return undefined;
}

function pruneExpired() {
  const now = Date.now();
  for (const [key, entry] of runContextCache.entries()) {
    if (now - entry.fetchedAtMs > entry.ttlMs) runContextCache.delete(key);
  }
}

export default function register(api) {
  if (!api || typeof api.on !== "function") return;

  const { cacheTtlMs } = getPluginConfig(api);

  safeLog(api, "info", "nbhd-cron-enforcement: registered (cron_changed + before_tool_call)");

  // ── cron_changed ───────────────────────────────────────────────────────
  api.on("cron_changed", (event) => {
    try {
      pruneExpired();
      const action = asTrimmedString(event && event.action);
      const sessionKey = event && event.sessionKey;
      const runId = event && event.runId;

      if (action === "finished" || action === "removed") {
        deleteCacheEntry(sessionKey, runId);
        return undefined;
      }
      if (action !== "started") return undefined;
      if (!asTrimmedString(sessionKey) && !asTrimmedString(runId)) return undefined;

      const job = asObject(event && event.job);
      const cronName = asTrimmedString(job.name || (event && event.jobId));
      const contract = parseContract(job.description);
      if (!contract) return undefined; // freeform/legacy/no-contract cron — nothing to enforce

      setCacheEntry(
        { fetchedAtMs: Date.now(), ttlMs: cacheTtlMs, cronName, contract, revisions: 0 },
        sessionKey,
        runId,
      );
      return undefined;
    } catch (error) {
      safeLogError(api, "warn", "nbhd-cron-enforcement: cron_changed error", error);
      return undefined;
    }
  });

  // ── before_tool_call ─────────────────────────────────────────────────────
  // FAIL-CLOSED runtime hook — the ENTIRE body is try/caught, returning
  // undefined on any error, and the first substantive check is the O(1)
  // cache-miss return (runs on EVERY tool call, for EVERY tenant, once
  // enabled — must be cheap and must never throw).
  api.on("before_tool_call", (event) => {
    try {
      const entry = lookupCacheEntry(event && event.sessionKey, event && event.runId);
      if (!entry || !entry.contract) return undefined;

      const dispatch = extractSendMessage(event);
      if (!dispatch.matched) return undefined;

      const decision = decideGuardAction(entry.contract, dispatch.message, entry.revisions);
      if (decision.type === "revise") {
        entry.revisions += 1;
        return { block: true, blockReason: decision.reason };
      }
      if (decision.type === "rewrite") {
        safeLog(
          api,
          "warn",
          `nbhd-cron-enforcement: rewriting outbound message cron=${entry.cronName} ` +
            `pattern=${(entry.contract && entry.contract.pattern) || "?"}`,
        );
        return { params: buildRewriteParams(event, dispatch, decision.content) };
      }
      if (decision.type === "allow") {
        safeLog(
          api,
          "warn",
          `nbhd-cron-enforcement: allowing outbound after revision budget exhausted ` +
            `cron=${entry.cronName} pattern=${(entry.contract && entry.contract.pattern) || "?"}`,
        );
        return undefined;
      }
      return undefined; // pass
    } catch (error) {
      safeLogError(api, "warn", "nbhd-cron-enforcement: before_tool_call guard error", error);
      return undefined;
    }
  });
}
