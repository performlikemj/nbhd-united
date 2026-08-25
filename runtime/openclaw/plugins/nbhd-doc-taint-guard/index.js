import { createHash } from "node:crypto";
import {
  wrapExternalContent,
  detectSuspiciousPatterns,
  looksAlreadyWrapped,
} from "../../external-content-wrap.js";

/**
 * NBHD Document Taint Guard
 *
 * Container-side implementation of P0-1 (instruction isolation) + P0-2
 * (egress taint gate) + P1-2 (injection telemetry) from
 * docs/upload-security-threat-model.md. Loads UNCONDITIONALLY (same shape
 * as nbhd-cron-enforcement) — the `pdf`/`image` tools are fleet-wide
 * (apps/orchestrator/tool_policy.py), so this guard must be too, independent
 * of `document_ingestion_enabled` (which only gates the separate
 * nbhd-document-keep retention plugin, off by default).
 *
 * Four hooks:
 *  - before_agent_run: mark the current turn "document-tainted" when the
 *    prompt carries `[Document attached:` / `[Photo attached:` — keyed by
 *    `runId` (unique per turn; NOT sessionKey, which spans many turns and
 *    would leak taint into unrelated later turns in the same conversation).
 *  - before_tool_call: (i) when the resolved real tool is `pdf`/`image`,
 *    remember its toolCallId → id for the tool_result_persist hook's
 *    authoritative correlation; (ii) on a tainted turn, block (enforce) or
 *    log (log_only) calls to the exfil-capable tools. Resolves the toolSearch
 *    meta-dispatch first: under `tools.toolSearch` (fleet default at
 *    OpenClaw 2026.5.28), `event.toolName` is the literal `"tool_call"`
 *    meta-tool; the real target sits in `event.params.id` (or
 *    `.toolId`/`.tool`/`.name`) — mirrors
 *    runtime/openclaw/plugins/nbhd-routing-context/index.js's
 *    `extractDispatchedToolId` (duplicated here, not imported, to keep this
 *    plugin free of cross-plugin coupling — keep both in sync if the
 *    dispatch envelope ever changes).
 *  - tool_result_persist: ALWAYS wrap pdf/image tool output in the
 *    instruction-isolation boundary (treat-as-data framing) before the model
 *    reads it on its next step within the same turn, and run
 *    detectSuspiciousPatterns over the raw text first, emitting a hash-only
 *    telemetry line (never the raw content — mirrors the
 *    doc_ingest_attached/pii_reuse discipline in apps/router/inbound_media.py
 *    / apps/pii/redactor.py). A fresh result whose text already looks wrapped
 *    is itself flagged (forged-trust attempt — see buildWrappedToolResultMessage)
 *    and STILL gets detected + wrapped, never skipped.
 *  - agent_end: clear this turn's taint entry so a long-lived tenant process
 *    doesn't accumulate state forever.
 *
 * Every hook handler fails OPEN (try/catch swallows unexpected errors and
 * returns undefined/pass) — a bug in this plugin must never brick a tool
 * call or drop a tool result fleet-wide. Mirrors nbhd-routing-context's
 * before_tool_call discipline.
 */

// ── Taint detection ──────────────────────────────────────────────────────

const DOCUMENT_MARKERS = ["[Document attached:", "[Photo attached:"];

/** Pure: does this turn's prompt carry an upload marker? */
export function promptIsDocumentTainted(prompt) {
  if (typeof prompt !== "string" || prompt.length === 0) return false;
  return DOCUMENT_MARKERS.some((marker) => prompt.includes(marker));
}

// ── toolSearch meta-dispatch unwrap ──────────────────────────────────────
// Mirrors nbhd-routing-context/index.js TOOL_DISPATCH_META / extractDispatchedToolId.

const TOOL_DISPATCH_META = "tool_call";

/** Pure: extract the real dispatched tool id from a tool_call's params. */
export function extractDispatchedToolId(params) {
  if (!params || typeof params !== "object") return "";
  const raw = params.id ?? params.toolId ?? params.tool ?? params.name ?? "";
  return typeof raw === "string" ? raw.trim().toLowerCase() : "";
}

/**
 * Pure: resolve the REAL tool identity a before_tool_call event targets,
 * whether the model called it directly or through the toolSearch
 * meta-dispatch wrapper.
 */
export function resolveRealToolId(event) {
  if (!event) return "";
  const toolName = typeof event.toolName === "string" ? event.toolName.trim().toLowerCase() : "";
  if (toolName === TOOL_DISPATCH_META) return extractDispatchedToolId(event.params);
  return toolName;
}

// ── Exfil gate decision ───────────────────────────────────────────────────

// Tools that read the extracted-text/vision-description path (P0-1 wrap target).
export const WRAP_SOURCE_TOOL_IDS = new Set(["pdf", "image"]);

// Tools an injected instruction could use to move data to the outside world
// (docs/upload-security-threat-model.md AC-5 / exfiltration surface inventory).
export const EXFIL_TOOL_IDS = new Set([
  "publish_portfolio_image",
  "nbhd_reddit_post",
  "nbhd_reddit_reply",
  "web_fetch",
]);

function describeExfilAction(realId) {
  switch (realId) {
    case "publish_portfolio_image":
      return "publish an image";
    case "nbhd_reddit_post":
      return "post to Reddit";
    case "nbhd_reddit_reply":
      return "reply on Reddit";
    case "web_fetch":
      return "fetch that URL";
    default:
      return "do that";
  }
}

function buildBlockReason(realId) {
  return (
    "A document or photo you uploaded this turn may contain hidden instructions. " +
    `I won't ${describeExfilAction(realId)} based on it without you explicitly asking in your own words. ` +
    "Ask me directly and I'll proceed."
  );
}

/**
 * Pure decision for the before_tool_call exfil gate. `mode` is "enforce" or
 * anything else (unset/"log_only"/unrecognized) behaves as log-only — the
 * safe default is to observe, never to silently start blocking on a bad
 * config value.
 *
 * Returns { realId, action: "ignore" | "log" | "block", result? }.
 */
export function decideExfilGate({ event, mode, tainted }) {
  const realId = resolveRealToolId(event);
  if (!realId || !EXFIL_TOOL_IDS.has(realId) || !tainted) {
    return { realId, action: "ignore" };
  }
  if (mode === "enforce") {
    return {
      realId,
      action: "block",
      result: { block: true, blockReason: buildBlockReason(realId) },
    };
  }
  return { realId, action: "log" };
}

// ── tool_result_persist wrap decision ────────────────────────────────────

// detectSuspiciousPatterns is telemetry-only (P1-2) — it does NOT gate the
// wrap below, which always covers the full block text, and it does NOT gate
// the egress taint gate (before_tool_call), which fires on the full turn
// regardless of what detection sees. But it runs on the SYNCHRONOUS
// tool_result_persist hot path with no timeout, and one of its upstream
// regexes (/\bexec\b.*command\s*=/i) backtracks catastrophically on a long
// single-line run with many "exec" tokens and no "command=" — an attacker-
// authored PDF/image page of text could freeze the WHOLE tenant container's
// Node event loop (assistant-silent / AC-8 DoS). The fail-open try/catch
// around the hook does NOT help here — nothing throws, it just spins.
//
// Measured on that adversarial pattern (doubling the input ~4x's the time —
// clean quadratic backtracking):
//   8,192 chars    ->     8ms
//   16,384 chars   ->    38ms
//   32,768 chars   ->   149ms  <- chosen cap: worst case ~150ms is an
//   65,536 chars   ->   633ms     acceptable synchronous hot-path budget
//   131,072 chars  ->  2,258ms
//   262,144 chars  ->  8,100-8,600ms  <- first attempt; still a real freeze
//   1,000,000 chars-> ~130,000ms (uncapped)
//
// Cap the text FED TO DETECTION ONLY; wrapping itself has no backtracking-
// prone regex (~78ms on 1MB) and always runs on the untruncated block.text,
// and the egress gate never depends on detection at all. The only thing lost
// at 32768 vs a larger cap: an injection pattern buried past the first 32KB
// of a very long single-line extraction won't trigger the
// `doc_injection_suspected` telemetry line for it — the isolation (wrap) and
// the egress block/log still apply regardless. Missing a telemetry signal is
// not the same as missing a control.
const MAX_DETECTION_SCAN_CHARS = 32768; // 32 KiB

/**
 * Pure: given the resolved real tool id for a toolResult message, decide
 * whether to wrap it and what the replacement content blocks should be.
 * Returns null if nothing should change. `onSuspicious(patternCount, scanText)`
 * is called once per suspicious text block for the caller to emit telemetry
 * (kept out of this pure function so it stays testable without a logger).
 * `scanText` is the (possibly capped) text actually scanned, not necessarily
 * the full block text — hashing/telemetry over it is fine since detection is
 * telemetry-only; the cap only trims tail telemetry, never the wrap.
 * `onPrewrapped(text)` is called when a FRESH result already looks wrapped
 * (see the comment inline below) — before the detect+wrap that always runs
 * regardless.
 */
export function buildWrappedToolResultMessage(message, realId, onSuspicious, onPrewrapped) {
  if (!WRAP_SOURCE_TOOL_IDS.has(realId)) return null;
  if (!message || !Array.isArray(message.content)) return null;
  let changed = false;
  const nextContent = message.content.map((block) => {
    if (!block || block.type !== "text" || typeof block.text !== "string") return block;
    // A FRESH pdf/image tool result should NEVER legitimately look already
    // wrapped — upstream OpenClaw doesn't wrap the upload path at all (that's
    // the whole reason this plugin exists), so nothing upstream of us could
    // have produced this shape. If it does, the only explanation is an
    // attacker's extracted text starting with the wrap's public, deterministic
    // prefix (WRAPPED_CONTENT_PREFIX in external-content-wrap.js) — forging a
    // fake "already trusted" boundary to try to dodge detection. Treat that as
    // suspicious in its own right (flag it), then ALWAYS detect + wrap anyway:
    // wrapExternalContent's own marker-sanitization turns the attacker's
    // forged `<<<EXTERNAL_UNTRUSTED_CONTENT` into `[[MARKER_SANITIZED]]`,
    // neutralizing the forgery. Double-wrapping here is the correct outcome,
    // not a bug — there is no legitimate case to skip wrapping a fresh result.
    if (looksAlreadyWrapped(block.text) && typeof onPrewrapped === "function") {
      onPrewrapped(block.text);
    }
    const scanText =
      block.text.length > MAX_DETECTION_SCAN_CHARS ? block.text.slice(0, MAX_DETECTION_SCAN_CHARS) : block.text;
    const suspicious = detectSuspiciousPatterns(scanText);
    if (suspicious.length > 0 && typeof onSuspicious === "function") {
      onSuspicious(suspicious.length, scanText);
    }
    changed = true;
    return { ...block, text: wrapExternalContent(block.text, { source: realId === "image" ? "image" : "document" }) };
  });
  if (!changed) return null;
  return { ...message, content: nextContent };
}

// ── Bounded map helpers ───────────────────────────────────────────────────
// Module-scope state lives for the lifetime of the tenant gateway process
// (register() runs once at boot). Two independent bounds keep it from
// growing unboundedly if a turn's agent_end never fires (crashed/aborted
// runs): (1) explicit cleanup on agent_end for taintedRuns, keyed by runId;
// (2) a plain insertion-order size cap for toolCallResolvedId, whose entries
// are also deleted the instant tool_result_persist consumes them (the normal
// case) — the cap only matters for calls whose result is never persisted
// (e.g. a tool error). A runId->toolCallIds index would let agent_end sweep
// toolCallResolvedId too, but adds a second index to keep in sync for a
// bound that's already covered by the size cap; not worth the complexity.
function setWithCap(map, key, value, cap) {
  if (map.size >= cap) {
    const oldestKey = map.keys().next().value;
    if (oldestKey !== undefined) map.delete(oldestKey);
  }
  map.set(key, value);
}

const MAX_TAINTED_RUNS = 2000;
const MAX_TRACKED_TOOL_CALLS = 500;

// Shared run-scoped state. The sub-agent bridge asks this exported seam before
// allowing sessions_spawn, closing the former "child gets a new runId" gap.
// agent_end remains the owner of cleanup.
const taintedRuns = new Map();

export function isDocumentTaintedRun(runId) {
  return typeof runId === "string" && runId.length > 0 && taintedRuns.get(runId) === true;
}

export default function register(api) {
  if (!api || typeof api.on !== "function") {
    return;
  }

  const cfg = api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
  const mode = cfg.mode === "enforce" ? "enforce" : "log_only";

  // runId -> true, for turns whose prompt carried an upload marker. The bridge
  // blocks sessions_spawn while this parent run is tainted, so the child-runId
  // boundary can no longer shed the taint before outward work.
  // toolCallId -> resolved real tool id ("pdf"/"image"), bridging
  // before_tool_call (which sees params.id) to tool_result_persist (which
  // doesn't).
  const toolCallResolvedId = new Map();

  api.logger.info(`NBHD document taint guard plugin registered (mode=${mode})`);

  api.on("before_agent_run", (event, ctx) => {
    try {
      const runId = ctx && ctx.runId;
      if (!runId) return undefined;
      if (promptIsDocumentTainted(event && event.prompt)) {
        setWithCap(taintedRuns, runId, true, MAX_TAINTED_RUNS);
      }
      return undefined;
    } catch (err) {
      try {
        api.logger.warn(`nbhd-doc-taint-guard: before_agent_run guard error: ${err}`);
      } catch (_ignored) {
        // logging must never turn a guard hiccup into a blocked run
      }
      return undefined;
    }
  });

  api.on("before_tool_call", (event, ctx) => {
    try {
      const realId = resolveRealToolId(event);
      const toolCallId = event && event.toolCallId;

      // (i) Track pdf/image calls so tool_result_persist can correlate,
      // regardless of taint — the wrap applies to every pdf/image read.
      if (realId && WRAP_SOURCE_TOOL_IDS.has(realId) && toolCallId) {
        setWithCap(toolCallResolvedId, toolCallId, realId, MAX_TRACKED_TOOL_CALLS);
      }

      // (ii) Exfil gate — only matters on a document-tainted turn.
      const runId = ctx && ctx.runId;
      const tainted = Boolean(runId && taintedRuns.get(runId));
      const decision = decideExfilGate({ event, mode, tainted });
      if (decision.action === "block") {
        api.logger.warn(`doc_exfil_blocked tool=${decision.realId} run=${runId ?? "?"}`);
        return decision.result;
      }
      if (decision.action === "log") {
        api.logger.warn(`doc_exfil_would_block tool=${decision.realId} run=${runId ?? "?"}`);
        return undefined;
      }
      return undefined;
    } catch (err) {
      try {
        api.logger.warn(`nbhd-doc-taint-guard: before_tool_call guard error: ${err}`);
      } catch (_ignored) {
        // logging must never turn a guard hiccup into a blocked call
      }
      return undefined;
    }
  });

  api.on("tool_result_persist", (event, ctx) => {
    try {
      const toolCallId = event && event.toolCallId;
      const mappedId = toolCallId ? toolCallResolvedId.get(toolCallId) : undefined;
      if (toolCallId && mappedId) toolCallResolvedId.delete(toolCallId); // one-shot correlation

      const observedToolName =
        event && typeof event.toolName === "string" ? event.toolName.trim().toLowerCase() : "";
      // TEMPORARY — tells us whether tool_result_persist's own toolName
      // already reflects the resolved real tool ("pdf"/"image") post-
      // dispatch, or still the "tool_call" meta-wrapper name. The wrap below
      // never depends on this being correct — it trusts the toolCallId map
      // from before_tool_call.
      // TODO: remove after canary confirms observedToolName.
      if (mappedId) {
        api.logger.info(
          `nbhd-doc-taint-guard: tool_result_persist toolCallId=${toolCallId ?? "?"} ` +
            `mappedId=${mappedId} observedToolName="${observedToolName}"`,
        );
      }

      const realId = mappedId || observedToolName;
      const mutated = buildWrappedToolResultMessage(
        event && event.message,
        realId,
        (patternCount, text) => {
          const hash = createHash("sha256").update(text).digest("hex").slice(0, 12);
          api.logger.warn(
            `doc_injection_suspected tool=${realId} pattern_count=${patternCount} content_sha256_prefix=${hash}`,
          );
        },
        (text) => {
          const hash = createHash("sha256").update(text).digest("hex").slice(0, 12);
          api.logger.warn(`doc_prewrapped_result tool=${realId} sha256_prefix=${hash}`);
        },
      );
      if (!mutated) return undefined;
      return { message: mutated };
    } catch (err) {
      try {
        api.logger.warn(`nbhd-doc-taint-guard: tool_result_persist guard error: ${err}`);
      } catch (_ignored) {
        // logging must never turn a guard hiccup into a dropped/corrupted result
      }
      return undefined;
    }
  });

  api.on("agent_end", (event) => {
    try {
      const runId = event && event.runId;
      if (runId) taintedRuns.delete(runId);
      return undefined;
    } catch (err) {
      try {
        api.logger.warn(`nbhd-doc-taint-guard: agent_end cleanup error: ${err}`);
      } catch (_ignored) {
        // best-effort cleanup only
      }
      return undefined;
    }
  });
}
