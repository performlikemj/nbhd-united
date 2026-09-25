// Shared declarative normalization contract, also interpreted by Python.
import fs from 'node:fs';
const NORMALIZATION = JSON.parse(fs.readFileSync(new URL('./cron-normalization.json', import.meta.url)));
export function normalizedOptionals(job) {
  const result=structuredClone(job);
  for(const [part,fields] of Object.entries(NORMALIZATION.nullAbsent)) {
    const target=part==='root'?result:result[part];
    if(target && typeof target==='object') for(const key of fields) if(target[key]===null) delete target[key];
  }
  // Mirror of the Python signer/normalizer: agentTurn top-level model pin travels as payload.model.
  const p=result.payload, model=result.model;
  if(typeof model==='string' && model && p && typeof p==='object' && p.kind==='agentTurn' && (p.model==null || p.model===model)) {
    p.model=model; delete result.model;
  }
  return result;
}
export function authoredAtMs(job) {
  const s=job.schedule||{}, values=[];
  for(const key of NORMALIZATION.instantFields) {
    const value=s[key]; if(value==null) continue;
    let instant;
    if(Number.isSafeInteger(value) && value>=0 && value<=8640000000000000) instant=value;
    else if(typeof value==='string' && new RegExp(NORMALIZATION.instantPattern).test(value)) instant=Date.parse(value);
    else return null;
    if(!Number.isFinite(instant)) return null;
    values.push(instant);
  }
  return values.length && values.every(v=>v===values[0])?values[0]:null;
}
export function normalizedDeclaration(job, pins = null) {
  job=normalizedOptionals(job);
  const s=structuredClone(job.schedule||{}), p=structuredClone(job.payload||{}), d=structuredClone(job.delivery||{});
  pins ??= Object.fromEntries(NORMALIZATION.timingPins.map(k=>[k,k in s]));
  for(const k of NORMALIZATION.timingPins) if(!pins[k]) delete s[k];
  if(s.kind==='at') {
    const due=authoredAtMs(job);
    for(const k of NORMALIZATION.instantFields) delete s[k];
    s.atMs=due; delete s.tz;
  }
  if(s.kind==='cron') s.tz ||= 'UTC';
  const alias=NORMALIZATION.aliases[p.kind];
  if(alias) {
    const value=alias.sources.map(k=>p[k]).find(v=>v!=null)??alias.default;
    for(const k of alias.sources) delete p[k];
    p[alias.target]=value;
  }
  for(const k of NORMALIZATION.payloadObservations) delete p[k];
  for(const [part,target] of [['payload',p],['delivery',d]])
    for(const [key,value] of Object.entries(NORMALIZATION.defaults[part])) if(!(key in target)) target[key]=structuredClone(value);
  for(const k of NORMALIZATION.lists) if(typeof p[k]==='string') p[k]=p[k].split(/[,\s]+/u).filter(Boolean);
  d.channel ||= 'last';
  const result=Object.fromEntries(Object.entries(NORMALIZATION.defaults.root).map(([k,v])=>[k,k in job?job[k]:structuredClone(v)]));
  return {...result,schedule:s,payload:p,delivery:d,
    sessionTarget:job.sessionTarget||(p.kind==='agentTurn'?'isolated':'main'),
    deleteAfterRun:job.deleteAfterRun??(s.kind==='at')};
}
export function stableJSON(value) {
  if (Array.isArray(value)) return '['+value.map(stableJSON).join(',')+']';
  if (value && typeof value === 'object') return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stableJSON(value[k])).join(',')+'}';
  return JSON.stringify(value);
}

const PROVEN_SHAPES = JSON.parse(fs.readFileSync(new URL('./cron-proven-shapes.json', import.meta.url)));
export function declarationShape(job,pins=null) {
  const result=normalizedDeclaration(job,pins);
  for(const path of NORMALIZATION.shapeVariables) {
    const parts=path.split('.'), key=parts.pop();
    let target=result;
    for(const part of parts) target=target[part]||{};
    if(key in target && target[key]!=='') {
      if(typeof target[key]==='string') target[key]={valueType:'string'};
      else if(Number.isSafeInteger(target[key])) target[key]={valueType:'integer'};
      else target[key]={valueType:'unsupported'};
    }
  }
  return result;
}
export function scalarTypesValid(job) {
  job=normalizedOptionals(job);
  for(const [part,fields] of Object.entries(NORMALIZATION.scalarTypes)) {
    const target=part==='root'?job:(job[part]??{});
    if(!target || typeof target!=='object' || Array.isArray(target)) return false;
    for(const [field,type] of Object.entries(fields)) {
      if(field in target && (type==='integer'?!Number.isSafeInteger(target[field]):typeof target[field]!==type)) return false;
    }
  }
  return true;
}
export function provenShape(job,pins=null) {
  if(!scalarTypesValid(job)) return false;
  const shape=stableJSON(declarationShape(job,pins));
  return PROVEN_SHAPES.shapes.some(entry=>stableJSON(entry.shape)===shape);
}
