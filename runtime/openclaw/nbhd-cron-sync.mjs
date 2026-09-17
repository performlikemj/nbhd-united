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
import { readFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";

const execFileP = promisify(execFile);

const ALLOWED_PAYLOAD_KINDS = new Set(["agentTurn", "systemEvent"]);
const DECL_PREFIX = "nbhd:";
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

// Epoch-ms fire time of an `at` schedule (ISO `at` or numeric `atMs`), or null.
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

// True when a container cron already matches the desired job on the fields we
// own (schedule, message, delivery), so we can skip a no-op re-add. Ignores
// runtime-added fields (anchorMs, state, configRevision, ...).
export function sameCron(desired, current) {
  const ds = desired.schedule || {};
  const cs = current.schedule || {};
  if (ds.kind !== cs.kind) return false;
  if (ds.kind === "every" && Number(ds.everyMs) !== Number(cs.everyMs)) return false;
  if (ds.kind === "cron" && String(ds.expr || "") !== String(cs.expr || "")) return false;
  if (ds.kind === "at" && atFireMs(ds) !== atFireMs(cs)) return false;
  if (String(ds.tz || "") !== String(cs.tz || "")) return false;
  const dp = desired.payload || {};
  const cp = current.payload || {};
  if (dp.kind !== cp.kind) return false;
  if (String(dp.message ?? dp.text ?? "") !== String(cp.message ?? cp.text ?? "")) return false;
  const dd = desired.delivery || {};
  const cd = current.delivery || {};
  if (String(dd.mode || "") !== String(cd.mode || "")) return false;
  return true;
}

// Build a SAFE `openclaw cron add` argv from extracted job fields. Returns null
// for anything unmappable. Only ever emits --message / --system-event.
export function buildAddArgs(job) {
  const decl = String(job.declarationKey || "");
  if (!decl.startsWith(DECL_PREFIX)) return null; // Django must stamp the key
  const name = String(job.displayName || job.name || decl);
  const args = ["cron", "add", name, "--declaration-key", decl];

  const s = job.schedule || {};
  if (s.kind === "every") {
    const d = msToDuration(s.everyMs);
    if (!d) return null;
    args.push("--every", d);
  } else if (s.kind === "cron" && s.expr) {
    args.push("--cron", String(s.expr));
    if (s.tz) args.push("--tz", String(s.tz));
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
  } else if (p.kind === "systemEvent") {
    args.push("--system-event", String(p.text ?? p.message ?? p.event ?? "heartbeat"));
  } else {
    return null;
  }

  // Delivery: our crons deliver via the agent's own nbhd_send_to_user call
  // (delivery.mode:"none"), so disable the runner's fallback delivery. Only an
  // explicit announce delivery re-enables it.
  const deliv = job.delivery || {};
  if (deliv.mode === "announce") {
    args.push("--announce");
    if (deliv.channel && deliv.channel !== "last") args.push("--channel", String(deliv.channel));
  } else {
    args.push("--no-deliver");
  }

  if (job.wakeMode === "now" || job.wakeMode === "next-heartbeat") args.push("--wake", job.wakeMode);
  if (typeof job.agentId === "string" && job.agentId) args.push("--agent", job.agentId);
  return args;
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

async function listNbhdCrons() {
  const out = await oc(["cron", "list", "--json"]);
  const doc = JSON.parse(out);
  const rows = Array.isArray(doc) ? doc : (doc && Array.isArray(doc.jobs) ? doc.jobs : []);
  return rows.filter((r) => r && typeof r.declarationKey === "string" && r.declarationKey.startsWith(DECL_PREFIX));
}

// One reconcile pass. Snapshots the container's current nbhd:* crons, then:
//  - adds a desired cron only when it is MISSING or CHANGED (so unchanged crons
//    are not re-added every poll — avoids fleet-wide cron.add churn);
//  - SKIPS one-shot `at` crons whose fire time has already passed (the container
//    rejects a past `schedule.at`, and a fired one-shot self-deletes — re-adding
//    it would spam "in the past" errors);
//  - removes nbhd:* crons the desired set no longer contains.
export async function reconcileOnce() {
  const jobs = await readSignedJobs();
  if (jobs === null) return { applied: 0, removed: 0, skipped: 0, ok: false };

  let current;
  try { current = await listNbhdCrons(); }
  catch (e) { warn("cron list failed, skipping this pass:", e && e.message); return { applied: 0, removed: 0, skipped: 0, ok: false }; }
  const currentByKey = new Map(current.map((r) => [r.declarationKey, r]));

  const now = Date.now();
  const desiredKeys = new Set();
  let applied = 0;
  let skipped = 0;
  for (const job of jobs) {
    if (job && job.enabled === false) continue;
    if (!isSafeJob(job)) { warn("REFUSED unsafe job:", job && job.name); skipped++; continue; }
    const schedule = job.schedule || {};
    if (schedule.kind === "at") {
      const fire = atFireMs(schedule);
      if (fire === null || fire <= now) { skipped++; continue; } // past/unparseable one-shot
    }
    const key = String(job.declarationKey || "");
    if (!key.startsWith(DECL_PREFIX)) { skipped++; continue; }
    desiredKeys.add(key);
    const existing = currentByKey.get(key);
    if (existing && sameCron(job, existing)) continue; // present + unchanged → no-op
    const args = buildAddArgs(job);
    if (!args) { warn("skip unmappable job:", job && job.name); skipped++; continue; }
    try { await oc(args); applied++; }
    catch (e) { warn("cron add failed for", job && job.name, "-", (e && e.message ? e.message : e)); }
  }

  let removed = 0;
  for (const [key, row] of currentByKey) {
    if (!desiredKeys.has(key)) {
      try { await oc(["cron", "rm", row.id]); removed++; }
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
