// Migration-only declaration comparison; never shipped in the OpenClaw image.
import { normalizedDeclaration, stableJSON, provenShape, normalizedOptionals } from './migration_cron_digest.mjs';
const ALLOWED_PAYLOAD_KINDS = new Set(["agentTurn", "systemEvent"]);
const DECL_PREFIX = "nbhd:";
const FIELDS = {
  "job": [
    "id",
    "jobId",
    "name",
    "displayName",
    "description",
    "declarationKey",
    "enabled",
    "schedule",
    "payload",
    "delivery",
    "sessionTarget",
    "sessionKey",
    "wakeMode",
    "agentId",
    "deleteAfterRun",
    "pacing",
    "state",
    "createdAt",
    "createdAtMs",
    "updatedAtMs",
    "nextRunAtMs",
    "runningAtMs",
    "status"
  ],
  "payload": [
    "kind",
    "message",
    "text",
    "event",
    "model",
    "fallbacks",
    "thinking",
    "timeoutSeconds",
    "toolsAllow",
    "toolsAllowIsDefault",
    "lightContext"
  ],
  "delivery": [
    "mode",
    "channel",
    "to",
    "accountId",
    "threadId",
    "bestEffort"
  ],
  "schedule": [
    "kind",
    "expr",
    "tz",
    "everyMs",
    "anchorMs",
    "at",
    "atMs",
    "staggerMs"
  ],
  "pacing": [
    "min",
    "max"
  ],
  "observation": [
    "configRevision",
    "effectiveAgentId"
  ]
};
export function supportedDeclaration(job) {
  if (!job || typeof job !== 'object' || Array.isArray(job)) return false;
  job = {...job};
  for (const field of FIELDS.observation) delete job[field];
  if (Number.isInteger(job.scheduledToolPolicy?.version) && stableJSON(job.scheduledToolPolicy) === stableJSON({version:1,mode:'trusted'})) delete job.scheduledToolPolicy;
  if (Object.keys(job).some(k=>!FIELDS.job.includes(k))) return false;
  for (const part of ['payload','delivery','schedule','pacing']) {
    if (Object.keys(job[part]||{}).some(k=>!FIELDS[part].includes(k))) return false;
  }
  return true;
}
// Match a command/script-bearing KEY (followed by a colon), so a plain reminder
// message whose text merely contains the word "command" is not a false positive.
const DENY_FIELD_RE = /"(command|commandArgv|command_argv|commandInput|commandCwd|commandEnv|script)"\s*:/i;
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

// Semantic comparison against real CLI rows: instants, empty defaults and
// canonical timing pins, not serialization of CLI argument strings.
export function sameCron(current, desired) {
  if (!buildAddArgs(current) || !buildAddArgs(desired) || !provenShape(desired)) return false;
  if (String(current.name || current.declarationKey) !== String(desired.name || desired.declarationKey)) return false;
  if ((current.displayName || current.name) !== (desired.displayName || desired.name)) return false;
  const pins = Object.fromEntries(['anchorMs','staggerMs'].map(k=>[k, desired.schedule?.[k] != null]));
  return stableJSON(normalizedDeclaration(current,pins)) === stableJSON(normalizedDeclaration(desired,pins));
}
