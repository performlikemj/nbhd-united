#!/usr/bin/env node
// In-container cron sync for OpenClaw 2026.9.4.
//
// WHY: 2026.9.4 gates the AGENT-tool gateway path (`POST /tools/invoke` with
// tool=cron): cron.list/add/update/remove/run require an "admitted operational
// run instance" or a signed agent-runtime-identity token. Django manages crons
// over HTTP with a bare Bearer token, so on 9.4 it can no longer push crons into
// the container — and the container's own scheduler is what fires them. The gate
// covers ONLY the agent path; the OPERATOR path (`openclaw cron add`, which wraps
// the operator RPC over the loopback gateway) is ungated. So Django writes a
// SIGNED nbhd-crons.json to the tenant's share and this helper applies it with
// the operator CLI. Once a job is in SQLite it fires as an admitted run, so
// delivery (nbhd_send_to_user) works.
//
// SECURITY (an OpenClaw cron payload can be `command`/`script` = shell on the
// gateway; the share is agent-reachable, so a prompt-injected agent could try to
// write a shell cron):
//   1. Payload allowlist (primary, key-independent): only payload.kind in
//      {agentTurn, systemEvent} is applied, and we build the CLI from extracted
//      fields — we ONLY ever emit --message / --system-event, never
//      --command/--script. A forged file cannot schedule shell.
//   2. HMAC-SHA256 signature (NBHD_INTERNAL_API_KEY): an unsigned/forged file is
//      ignored. Django (holding the key) is the only valid signer.
//   3. Namespacing: only `nbhd:*` declarations are added/removed here; operator-
//      and agent-created crons are never touched.

import { createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";

const execFileP = promisify(execFile);

const ALLOWED_PAYLOAD_KINDS = new Set(["agentTurn", "systemEvent"]);
const DECL_PREFIX = "nbhd:";
const FIELDS = JSON.parse(readFileSync(new URL('./cron-declaration-fields.json', import.meta.url), 'utf8'));
export function supportedDeclaration(job) {
  if (!job || Object.keys(job).some(k=>!FIELDS.job.includes(k))) return false;
  for (const part of ['payload','delivery','schedule','pacing']) {
    if (Object.keys(job[part]||{}).some(k=>!FIELDS[part].includes(k))) return false;
  }
  return true;
}
// Match a command/script-bearing KEY (followed by a colon), so a plain reminder
// message whose text merely contains the word "command" is not a false positive.
const DENY_FIELD_RE = /"(command|commandArgv|command_argv|commandInput|commandCwd|commandEnv|script)"\s*:/i;
const POLL_MS = Number(process.env.NBHD_CRON_SYNC_POLL_MS || 25000);

const CONFIG_PATH = process.env.OPENCLAW_CONFIG_PATH || "/home/node/.openclaw/openclaw.json";
const CRONS_FILE = process.env.NBHD_CRONS_FILE || path.join(path.dirname(CONFIG_PATH), "nbhd-crons.json");
const OC_BIN = process.env.OPENCLAW_BIN || "openclaw";
const KEY = process.env.NBHD_INTERNAL_API_KEY || "";

function log(...a) { console.log("[nbhd:cron-sync]", ...a); }
function warn(...a) { console.warn("[nbhd:cron-sync] WARN", ...a); }

// Read + verify the signed crons file. Returns the jobs array, or null when the
// file is absent, malformed, unsigned, or fails signature verification (any of
// which means "apply nothing" — we never fall back to trusting unsigned data).
export async function readSignedJobs() {
  if (!KEY) { warn("no NBHD_INTERNAL_API_KEY in env — refusing to apply any crons"); return null; }
  let raw;
  try { raw = await readFile(CRONS_FILE, "utf8"); }
  catch (e) { if (e && e.code === "ENOENT") return null; warn("read failed:", e.message); return null; }
  let doc;
  try { doc = JSON.parse(raw); } catch { warn("crons file is not valid JSON — ignoring"); return null; }
  const signed = doc && doc.signed;
  const sig = doc && doc.sig;
  if (typeof signed !== "string" || typeof sig !== "string" || !/^[0-9a-f]{64}$/i.test(sig)) {
    warn("crons file missing a well-formed {signed, sig} — ignoring"); return null;
  }
  const expected = createHmac("sha256", KEY).update(signed).digest("hex");
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(sig.toLowerCase(), "utf8");
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    warn("crons file signature INVALID — ignoring (not written by Django)"); return null;
  }
  let jobs;
  try { jobs = JSON.parse(signed); } catch { warn("signed payload is not valid JSON — ignoring"); return null; }
  if (!Array.isArray(jobs)) { warn("signed payload is not a jobs array — ignoring"); return null; }
  return jobs;
}

// Payload allowlist — the primary, key-independent control. Rejects any job that
// could run shell, regardless of signature.
export function isSafeJob(job) {
  if (!job || typeof job !== "object") return false;
  const payload = job.payload;
  if (!payload || typeof payload !== "object") return false;
  if (!ALLOWED_PAYLOAD_KINDS.has(payload.kind)) return false;
  if (DENY_FIELD_RE.test(JSON.stringify(job))) return false; // belt-and-suspenders
  return true;
}

export function msToDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 1000) return null;
  if (n % 86400000 === 0) return `${n / 86400000}d`;
  if (n % 3600000 === 0) return `${n / 3600000}h`;
  if (n % 60000 === 0) return `${n / 60000}m`;
  if (n % 1000 === 0) return `${n / 1000}s`;
  return null;
}

export function atFireMs(schedule) {
  if (!schedule || typeof schedule !== "object") return null;
  if (Number.isFinite(schedule.atMs)) return Number(schedule.atMs);
  if (typeof schedule.at === "number") return schedule.at;
  if (typeof schedule.at === "string") {
    const t = Date.parse(schedule.at);
    return Number.isFinite(t) ? t : null;
  }
  return null;
}

// Build a SAFE `openclaw cron add` argv from extracted job fields. Returns null
// for anything unmappable. Only ever emits --message / --system-event.
export function buildAddArgs(job) {
  if (!isSafeJob(job) || !supportedDeclaration(job)) return null;
  const decl = String(job.declarationKey || "");
  if (!decl.startsWith(DECL_PREFIX)) return null; // Django must stamp the key
  const name = String(job.name || decl);
  const args = ["cron", "add", name, "--declaration-key", decl];

  if (job.displayName != null) args.push('--display-name', String(job.displayName));
  const s = job.schedule || {};
  if (s.kind === "every") {
    const d = msToDuration(s.everyMs);
    if (!d) return null;
    args.push("--every", d);
  } else if (s.kind === "cron" && s.expr) {
    args.push("--cron", String(s.expr));
    if (s.tz) args.push("--tz", String(s.tz));
    if (s.staggerMs === 0) args.push('--exact');
    else if (s.staggerMs != null) {
      const stagger = msToDuration(s.staggerMs);
      if (!stagger) return null;
      args.push('--stagger', stagger);
    }
  } else if (s.kind === "at") {
    let at = s.at;
    if (at == null && Number.isFinite(s.atMs)) at = new Date(Number(s.atMs)).toISOString();
    if (typeof at === "number") at = new Date(at).toISOString();
    if (!at) return null;
    args.push("--at", String(at));
    if (s.tz) args.push("--tz", String(s.tz));
  } else {
    return null;
  }

  const p = job.payload || {};
  if (p.kind === "agentTurn") {
    const msg = p.message ?? p.text ?? "";
    if (!String(msg).trim()) return null;
    args.push("--message", String(msg));
    // Individual flags from 2026.9.4 registerCronMutationOptions. --json is
    // output-only. These are agentTurn parameters, never new payload kinds.
    if (p.thinking != null) args.push("--thinking", String(p.thinking));
    if (p.model != null) args.push("--model", String(p.model));
    if (p.fallbacks != null) args.push("--fallbacks", cliList(p.fallbacks));
    if (p.timeoutSeconds != null) args.push("--timeout-seconds", String(p.timeoutSeconds));
    if (p.toolsAllow != null) {
      const tools = cliList(p.toolsAllow);
      // The CLI turns an empty list into undefined (default tools). Refuse
      // instead of widening a declaration that explicitly allows no tools.
      if (!tools.split(/[,\s]+/u).some(Boolean)) return null;
      args.push("--tools", tools);
    }
    if (p.lightContext === true) args.push("--light-context");
    // cron ADD has no --no-light-context (EDIT only); false becomes absent.
    // Likewise --tools "" cannot express an explicit empty allowlist in 9.4.
  } else if (p.kind === "systemEvent") {
    if (Object.keys(p).some(k=>!['kind','text','message','event','toolsAllow','toolsAllowIsDefault'].includes(k))) return null;
    args.push("--system-event", String(p.text ?? p.message ?? p.event ?? "heartbeat"));
    if (p.toolsAllow != null) args.push('--tools', cliList(p.toolsAllow));
  } else {
    return null;
  }

  // Delivery: our crons deliver via the agent's own nbhd_send_to_user call
  // (delivery.mode:"none"), so disable the runner's fallback delivery. Only an
  // explicit announce delivery re-enables it.
  const deliv = job.delivery || {};
  if (!['none','announce','webhook'].includes(deliv.mode || 'none')) return null;
  if (deliv.mode === 'webhook') {
    if (!/^https?:\/\//.test(deliv.to||'') || ['channel','accountId','threadId'].some(k=>deliv[k]!=null)) return null;
    args.push('--webhook', deliv.to);
  } else if (p.kind === 'agentTurn') {
    args.push(deliv.mode === 'announce' ? '--announce' : '--no-deliver');
    if (deliv.channel && deliv.channel !== 'last') args.push('--channel', String(deliv.channel));
    for (const [field,flag] of [['to','--to'],['accountId','--account'],['threadId','--thread-id']]) {
      if (deliv[field] != null) args.push(flag, String(deliv[field]));
    }
  } else if (deliv.mode === 'announce') return null;
  if (deliv.bestEffort === true) args.push('--best-effort-deliver');
  args.push('--session', job.sessionTarget || (p.kind === 'agentTurn' ? 'isolated' : 'main'));
  if (job.sessionKey != null) args.push('--session-key', String(job.sessionKey));
  const deleteAfter = job.deleteAfterRun ?? (s.kind === 'at');
  args.push(deleteAfter ? '--delete-after-run' : '--keep-after-run');
  for (const field of ['min','max']) {
    if (job.pacing?.[field] != null) args.push('--pacing-'+field, String(job.pacing[field]));
  }

  if (job.wakeMode === "now" || job.wakeMode === "next-heartbeat") args.push("--wake", job.wakeMode);
  if (typeof job.agentId === "string" && job.agentId) args.push("--agent", job.agentId);
  if (job.description != null) args.push("--description", String(job.description));
  return args;
}

function cliList(value) {
  return Array.isArray(value) ? value.join(",") : String(value);
}

// Compare exactly the fields this adapter can apply, including all agentTurn
// controls and the enforcement contract in description. Runtime timestamps and
// schedule anchors are intentionally ignored by the argv projection.
export function sameCron(current, desired) {
  const normalize = (job) => {
    if (!job) return null;
    const copy = structuredClone(job);
    if (desired?.schedule?.anchorMs == null && copy.schedule) delete copy.schedule.anchorMs;
    if (desired?.schedule?.staggerMs == null && copy.schedule) delete copy.schedule.staggerMs;
    // Leave the container's wake mode alone unless the declaration pins it.
    if (desired?.wakeMode == null) delete copy.wakeMode;
    else copy.wakeMode ||= "now";
    // OpenClaw supplies its default tool policy when the declaration omits
    // toolsAllow: the "all tools" wildcard ["*"], and in some builds a
    // toolsAllowIsDefault marker. `cron list` echoes ["*"] back WITHOUT the
    // marker, so a bare cron's current row carries toolsAllow:["*"] while its
    // declaration has none — match on the wildcard too, or every bare cron
    // re-adds each poll. A declaration that pins a real (non-wildcard) list
    // still compares, so removing a pinned policy is still detected.
    const ct = copy.payload?.toolsAllow;
    const ctIsDefault =
      copy.payload?.toolsAllowIsDefault || (Array.isArray(ct) && ct.length === 1 && ct[0] === "*");
    if (desired?.payload?.toolsAllow == null && ctIsDefault) {
      delete copy.payload.toolsAllow;
    }
    return buildAddArgs(copy);
  };
  const a = normalize(current);
  const b = normalize(desired);
  return a !== null && b !== null && JSON.stringify(a) === JSON.stringify(b);
}

async function oc(args) {
  // Clear NODE_OPTIONS for the spawned CLI so the global redact-stdout shim
  // (NODE_OPTIONS=--require .../redact-stdout.js) does not prepend its
  // "[nbhd:redact...]" marker to the child's stdout — which would corrupt
  // `cron list --json`. The child's stdout is consumed here, not logged, so it
  // needs no redaction.
  const env = { ...process.env, NODE_OPTIONS: "" };
  const { stdout } = await execFileP(OC_BIN, args, { env, timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
  return stdout;
}

async function listNbhdDeclarations(run) {
  const out = await run(["cron", "list", "--json"]);
  const doc = JSON.parse(out);
  const rows = Array.isArray(doc) ? doc : (doc && Array.isArray(doc.jobs) ? doc.jobs : []);
  return rows
    .filter((r) => r && typeof r.declarationKey === "string" && r.declarationKey.startsWith(DECL_PREFIX));
}

// One reconcile pass: upsert missing/changed nbhd:* crons, then remove nbhd:*
// crons that are no longer desired. A failed upsert retains the existing job
// for the next retry. The injected runner lets tests exercise this without RPC.
export async function reconcileOnce({ run = oc } = {}) {
  const jobs = await readSignedJobs();
  if (jobs === null) return { applied: 0, removed: 0, skipped: 0, ok: false };

  let current;
  try { current = await listNbhdDeclarations(run); }
  catch (e) {
    warn("cron list failed, skipping reconcile:", e && e.message);
    return { applied: 0, removed: 0, skipped: 0, ok: false };
  }
  const currentByKey = new Map(current.map(row => [row.declarationKey, row]));

  const desiredKeys = new Set();
  let applied = 0;
  let skipped = 0;
  for (const job of jobs) {
    if (job && job.enabled === false) continue;
    if (!isSafeJob(job)) { warn("REFUSED unsafe job:", job && job.name); skipped++; continue; }
    const sched = job.schedule || {};
    if (sched.kind === "at") {
      const fire = atFireMs(sched);
      if (fire === null || fire <= Date.now()) { skipped++; continue; }
    }
    const args = buildAddArgs(job);
    if (!args) { warn("skip unmappable job:", job && job.name); skipped++; continue; }
    desiredKeys.add(String(job.declarationKey));
    if (sameCron(currentByKey.get(job.declarationKey), job)) continue;
    try {
      await run(args);
      applied++;
    } catch (e) {
      warn("cron add failed for", job && job.name, "-", (e && e.message ? e.message : e));
    }
  }

  let removed = 0;
  for (const row of current) {
    if (!desiredKeys.has(row.declarationKey)) {
      try { await run(["cron", "rm", row.id]); removed++; }
      catch (e) { warn("cron rm failed for", row.id, "-", e && e.message); }
    }
  }
  return { applied, removed, skipped, ok: true };
}

async function main() {
  const once = process.argv.includes("--once");
  if (once) {
    const r = await reconcileOnce();
    if (r.ok) log(`synced: applied=${r.applied} removed=${r.removed} skipped=${r.skipped}`);
    return;
  }
  log(`starting poll loop (every ${POLL_MS}ms), file=${CRONS_FILE}`);
  for (;;) {
    try {
      const r = await reconcileOnce();
      if (r.ok && (r.applied || r.removed || r.skipped)) {
        log(`synced: applied=${r.applied} removed=${r.removed} skipped=${r.skipped}`);
      }
    } catch (e) {
      warn("reconcile pass threw:", e && e.message ? e.message : e);
    }
    await new Promise((res) => setTimeout(res, POLL_MS));
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => { warn("fatal:", e && e.message ? e.message : e); process.exit(1); });
}
