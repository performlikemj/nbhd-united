// Private comparison only; never shipped in the image or emitted in reports.
export function normalizedDeclaration(job, pins = {}) {
  const s = structuredClone(job.schedule || {}), p = structuredClone(job.payload || {}), d = structuredClone(job.delivery || {});
  for (const k of ['anchorMs','staggerMs']) if (!pins[k]) delete s[k];
  if (s.kind === 'at') { s.atMs = Number.isFinite(s.atMs) ? s.atMs : (typeof s.at === 'number' ? s.at : Date.parse(s.at)); delete s.at; delete s.tz; }
  if (s.kind === 'cron') s.tz ||= 'UTC';
  if (p.kind === 'agentTurn') { p.message = p.message ?? p.text ?? ''; delete p.text; }
  else { p.text = p.text ?? p.message ?? p.event ?? 'heartbeat'; delete p.message; delete p.event; }
  delete p.toolsAllowIsDefault;
  p.toolsAllow ??= ['*'];
  for (const k of ['toolsAllow','fallbacks']) if (typeof p[k] === 'string') p[k] = p[k].split(/[,\s]+/u).filter(Boolean);
  p.lightContext ??= false;
  for (const [k,v] of Object.entries({mode:'none',channel:'last',to:'',accountId:'',threadId:'',bestEffort:false})) d[k] ??= v;
  d.channel ||= 'last';
  return {schedule:s,payload:p,delivery:d,enabled:job.enabled??true,
    sessionTarget:job.sessionTarget||(p.kind==='agentTurn'?'isolated':'main'),sessionKey:job.sessionKey||'',wakeMode:job.wakeMode||'now',
    deleteAfterRun:job.deleteAfterRun??(s.kind==='at'),agentId:job.agentId||'',description:job.description||'',pacing:job.pacing||{}};
}
export function stableJSON(value) {
  if (Array.isArray(value)) return '['+value.map(stableJSON).join(',')+']';
  if (value && typeof value === 'object') return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stableJSON(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
