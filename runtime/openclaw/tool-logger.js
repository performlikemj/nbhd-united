"use strict";

/**
 * Uniform tool-call logging wrapper for nbhd-* OpenClaw plugins.
 *
 * Why this exists:
 * The stdout/stderr redaction sidecar in `runtime/openclaw/redact-stdout.js`
 * passes "operational" lines (bracket-prefixed, ISO-timestamped, etc.) and
 * drops everything else as "non-operational" tenant content. Each nbhd-*
 * plugin previously called `console.log("foo failed: " + err)` directly,
 * producing lines that didn't match the operational classifier — the
 * redactor swallowed them. Effect: we couldn't tell whether a tool was
 * registered, whether the agent invoked it during a cron, or how the
 * call ended.
 *
 * This wrapper standardizes all tool boundaries on a single bracket-
 * prefixed format that the redactor passes naturally:
 *
 *   [nbhd:tools] registered tool=<name> plugin=<plugin>
 *   [nbhd:tools] call <name> id=<callId>
 *   [nbhd:tools] ok <name> id=<callId> duration=<ms>ms
 *   [nbhd:tools] error <name> id=<callId> duration=<ms>ms message=<errMessage>
 *
 * Deliberately omits args/results/payload. Tenant content stays out of
 * stdout — the redactor would mask it anyway, and there's no value in
 * letting it leak even briefly between plugin and redactor.
 *
 * Usage in each plugin:
 *
 *   const { wrapTool, logToolRegistered } = require("../../tool-logger");
 *   // ...
 *   const TOOL_DEF = wrapTool({
 *     name: "nbhd_send_to_user",
 *     description: "...",
 *     parameters: { ... },
 *     async execute(id, params) { ... },
 *   }, { plugin: "nbhd-journal-tools" });
 *   api.registerTool(TOOL_DEF);
 *   logToolRegistered(TOOL_DEF.name, "nbhd-journal-tools");
 *
 * Test surface: `tool-logger.test.js` covers start/ok/error/duration/no-payload.
 */

let __callCounter = 0;
function nextCallId() {
  // Monotonic per-process counter — keeps log correlation cheap and
  // human-readable without pulling in a UUID dep. Wraps at 2^53 in
  // theory; we'll redeploy before then.
  __callCounter = (__callCounter + 1) | 0;
  return String(__callCounter);
}

function safeErrorMessage(err) {
  if (!err) return "(unknown)";
  if (typeof err === "string") return err.slice(0, 200);
  const msg = err.message ?? String(err);
  // Collapse whitespace + trim so multi-line errors stay on one log line,
  // then cap length so an LLM-style error blob doesn't dominate the log.
  return String(msg).replace(/\s+/g, " ").trim().slice(0, 200);
}

function requiredList(parameters) {
  const req = parameters && parameters.required;
  return Array.isArray(req) ? req.filter((k) => typeof k === "string") : [];
}

/**
 * Resolve the params object from an execute(...) call's args, tolerant of both
 * argument conventions present in this codebase:
 *   - canonical OpenClaw: execute(toolCallId, params, signal, onUpdate) → args[1]
 *   - a few legacy nbhd tools (nbhd-image-gen, finance gravity_query):
 *     execute(params) / execute({...}) → args[0]
 * We check args[1] first, then args[0], and NEVER fall through to args[2]+
 * (those hold the AbortSignal / onUpdate callback, never params). This makes
 * the required-arg guard safe regardless of how an individual tool destructures
 * its signature — it always validates the object OpenClaw actually passed.
 */
function paramsFromArgs(args) {
  const isPlainObject = (v) => v != null && typeof v === "object" && !Array.isArray(v);
  if (isPlainObject(args[1])) return args[1];
  if (isPlainObject(args[0])) return args[0];
  return {};
}

/**
 * Required-parameter presence check. Returns the list of required keys that
 * the model OMITTED entirely (undefined/null). Deliberately presence-only —
 * it does NOT reject empty strings/arrays, so it can never newly reject a
 * call that the tool's own execute() previously accepted; the per-tool
 * `if (!x) throw` checks remain the backstop for empty values. This exists
 * to turn the observed "model omits a required arg" failures (canary
 * 2026-06-13: section_slug/content/text/task_id) into ONE uniform error that
 * names every required field, so the model fixes them all in a single retry.
 */
function missingRequiredParams(parameters, params) {
  const req = requiredList(parameters);
  if (req.length === 0) return [];
  const obj = params && typeof params === "object" ? params : {};
  return req.filter((key) => obj[key] === undefined || obj[key] === null);
}

/**
 * Wrap a tool definition so its execute() emits uniform tool-boundary
 * log lines. Returns a NEW object so plugins can pass it straight to
 * api.registerTool without mutating their own constants.
 *
 * @param {object} toolDef - { name, description, parameters, execute }
 * @param {object} [opts]  - { plugin: string } for log attribution
 * @returns {object} wrapped tool definition
 */
function wrapTool(toolDef, opts = {}) {
  if (!toolDef || typeof toolDef !== "object") {
    throw new TypeError("wrapTool: toolDef must be an object");
  }
  const name = toolDef.name;
  if (typeof name !== "string" || !name) {
    throw new TypeError("wrapTool: toolDef.name must be a non-empty string");
  }
  const originalExecute = toolDef.execute;
  if (typeof originalExecute !== "function") {
    throw new TypeError(`wrapTool: ${name}.execute must be a function`);
  }

  // Emit registration log immediately so the boot logs show the
  // available toolset (useful for "did this plugin actually load?"
  // diagnosis without exec'ing into the container). Triggered at wrap
  // time rather than register time on the assumption — enforced by
  // convention in the nbhd-* plugins — that wrapTool() output is passed
  // straight to api.registerTool().
  if (opts.plugin) {
    console.log(`[nbhd:tools] registered tool=${name} plugin=${opts.plugin}`);
  }

  // Variadic so we don't constrain plugin execute() signatures. The
  // canonical OpenClaw 5.7 signature is
  // `(toolCallId, params, signal?, onUpdate?)` — see
  // `plugin-sdk/src/agents/tools/common.d.ts` ErasedAgentToolExecute —
  // but a few legacy plugins destructure the first arg directly. We
  // pass everything through unchanged.
  async function wrappedExecute(...args) {
    const callId = nextCallId();
    const start = Date.now();
    console.log(`[nbhd:tools] call ${name} id=${callId}`);

    // Schema-driven required-arg guard. Fails fast with ONE complete message
    // naming every omitted required field (and the full required set), instead
    // of the first-gap-only `<x> is required` each tool throws. Presence-only,
    // so well-formed calls are unaffected. Resolves params from args[1]/args[0]
    // (see paramsFromArgs) so it's correct for both signature conventions.
    const missing = missingRequiredParams(toolDef.parameters, paramsFromArgs(args));
    if (missing.length > 0) {
      const duration = Date.now() - start;
      const err = new Error(
        `missing required parameter(s): ${missing.join(", ")}. ` +
          `${name} requires: ${requiredList(toolDef.parameters).join(", ")}. ` +
          `Re-call ${name} with every required parameter set.`,
      );
      console.log(
        `[nbhd:tools] error ${name} id=${callId} duration=${duration}ms message=${safeErrorMessage(err)}`,
      );
      throw err;
    }

    try {
      const result = await originalExecute.apply(this, args);
      const duration = Date.now() - start;
      console.log(`[nbhd:tools] ok ${name} id=${callId} duration=${duration}ms`);
      return result;
    } catch (err) {
      const duration = Date.now() - start;
      console.log(
        `[nbhd:tools] error ${name} id=${callId} duration=${duration}ms message=${safeErrorMessage(err)}`,
      );
      throw err;
    }
  }

  return { ...toolDef, execute: wrappedExecute };
}

/**
 * Log a tool registration. Call this once per tool right after
 * api.registerTool succeeds, so the boot logs show the available toolset
 * (useful when diagnosing "did this plugin actually load?" without
 * exec'ing into the container).
 *
 * @param {string} name   - The tool name
 * @param {string} plugin - The plugin name (for attribution)
 */
function logToolRegistered(name, plugin) {
  console.log(`[nbhd:tools] registered tool=${name} plugin=${plugin}`);
}

module.exports = {
  wrapTool,
  logToolRegistered,
  // Exported for unit tests only.
  __internals: { safeErrorMessage, nextCallId },
};
