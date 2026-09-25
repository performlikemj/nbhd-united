"""Bounded, metadata-only operator access to the running OpenClaw replica.

Uses the same authenticated console protocol as Azure CLI's containerapp exec.
No command output, credential, config or reminder payload is logged on errors.
"""

import json
import re
import shlex
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests
import websocket
from django.conf import settings

from .azure_client import get_container_client, is_mock


class OperatorError(RuntimeError):
    pass


def _connection(tenant):
    if is_mock():
        raise OperatorError("Operator verification is unavailable with AZURE_MOCK=true")
    client = get_container_client()
    app = client.container_apps.get(settings.AZURE_RESOURCE_GROUP, tenant.container_id)
    if not app.latest_ready_revision_name or app.latest_ready_revision_name != app.latest_revision_name:
        raise OperatorError("Latest revision is not ready")
    replicas = client.container_apps_revision_replicas.list_replicas(
        settings.AZURE_RESOURCE_GROUP, tenant.container_id, app.latest_ready_revision_name
    )
    containers = [c for r in replicas.value for c in (r.containers or []) if c.name == "openclaw" and c.ready]
    if len(containers) != 1:
        raise OperatorError("Expected exactly one ready OpenClaw replica")
    token = client.container_apps.get_auth_token(settings.AZURE_RESOURCE_GROUP, tenant.container_id).token
    return containers[0], token


def run_node(tenant, body: str, *, timeout: int = 90):
    """Execute a fixed JS adapter; only its explicit JSON result leaves the replica."""
    container, token = _connection(tenant)
    # Existing 9.4 images may contain the pre-review comparator. Supply the
    # controller's pinned pure adapter functions for verification/restoration.
    # This contains code only; cron payloads still stay inside the replica.
    if "sameCron,buildAddArgs" in body or "readSignedJobs,sameCron" in body:
        source = (Path(settings.BASE_DIR) / "runtime/openclaw/nbhd-cron-sync.mjs").read_text()
        fields = (Path(settings.BASE_DIR) / "runtime/openclaw/cron-declaration-fields.json").read_text()
        constants = source[source.index("const ALLOWED_PAYLOAD_KINDS") : source.index("const POLL_MS")]
        constants = re.sub(r"const FIELDS = .*?;", "const FIELDS = " + fields + ";", constants)
        pure = source[source.index("export function isSafeJob") : source.index("async function oc(")].replace(
            "export ", ""
        )
        adapter = constants.replace("export ", "") + pure
        apply = source[
            source.index("export async function applyCron") : source.index("async function listNbhdDeclarations")
        ].replace("export ", "")
        body = body.replace(
            "const {readSignedJobs,sameCron,buildAddArgs,atFireMs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');",
            "const {readSignedJobs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');" + adapter,
        )
        body = body.replace("const {sameCron,buildAddArgs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');", adapter)
        body = body.replace(
            "const {sameCron,buildAddArgs,applyCron}=await import('/opt/nbhd/nbhd-cron-sync.mjs');", adapter + apply
        )
    script = (
        "const {execFileSync}=require('node:child_process');"
        "const fs=require('node:fs');const crypto=require('node:crypto');"
        f"const deadline=Date.now()+{max(1000, (timeout - 5) * 1000)};"
        "const oc=(args)=>{const remaining=deadline-Date.now();if(remaining<=0)throw Error('deadline');"
        "return execFileSync('openclaw',args,{encoding:'utf8',maxBuffer:8*1024*1024,"
        "env:{...process.env,NODE_OPTIONS:''},timeout:Math.min(30000,remaining),stdio:['ignore','pipe','pipe']});};"
        "(async()=>{" + body + "})().then(result=>console.log('NBHD_RESULT:'+JSON.stringify(result)))"
        ".catch(()=>console.log('NBHD_RESULT:'+JSON.stringify({operatorError:true})));"
    )
    command = "env NODE_OPTIONS= node -e " + shlex.quote(script)
    endpoint = container.exec_endpoint
    if not endpoint or not endpoint.startswith("wss://"):
        raise OperatorError("Missing secure console endpoint")
    sock = websocket.create_connection(
        endpoint + "?command=" + quote_plus(command),
        header=[f"Authorization: Bearer {token}"],
        timeout=timeout,
    )
    output = b""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = sock.recv()
            if not chunk:
                break
            if not isinstance(chunk, bytes) or chunk[0] == 2:
                raise OperatorError("Console protocol failure")
            if chunk[:2] not in (b"\x00\x01", b"\x00\x02"):
                continue
            output += chunk[2:]
            if len(output) > 2 * 1024 * 1024:
                raise OperatorError("Operator response exceeded metadata limit")
            match = re.search(rb"(?:^|[\r\n])NBHD_RESULT:([^\r\n]+)[\r\n]", output)
            if match:
                result = json.loads(match[1])
                if isinstance(result, dict) and result.get("operatorError"):
                    raise OperatorError("Operator command failed (output withheld)")
                return result
    except (websocket.WebSocketException, ValueError) as exc:
        raise OperatorError("Operator console failed (output withheld)") from None
    finally:
        sock.close()
    raise OperatorError("Operator command timed out or returned no result")


# Kept payload-free: even names can contain user prose, so callers print counts only.
_LIST = """
const doc=JSON.parse(oc(['cron','list','--all','--json']));
const jobs=Array.isArray(doc)?doc:doc.jobs;
if(!Array.isArray(jobs)) throw Error('shape');
if(doc.hasMore===true || Number(doc.total||0)>jobs.length ||
    (doc.nextOffset!=null && doc.nextOffset!==false)) throw Error('incomplete list');
"""


def list_crons(tenant) -> list[dict]:
    return run_node(
        tenant,
        _LIST
        + """
return jobs.map(j=>({id:j.id||j.jobId,name:j.name,declarationKey:j.declarationKey||'',
    enabled:j.enabled!==false,schedule:j.schedule,state:{nextRunAtMs:j.state?.nextRunAtMs,runningAtMs:j.state?.runningAtMs,lastDelivered:j.state?.lastDelivered,lastRunAtMs:j.state?.lastRunAtMs}}));
""",
    )


def set_cron_enabled(tenant, job_id: str, enabled: bool):
    return run_node(
        tenant,
        "oc(['cron'," + json.dumps("enable" if enabled else "disable") + "," + json.dumps(job_id) + "]);return true;",
    )


def inspect_signed_crons(tenant, *, cleanup: bool = False) -> dict:
    """Compare in-container desired jobs, remove only proven legacy duplicates.

    Comparison includes payloads INSIDE the replica via the image's tested adapter.
    Only IDs, declaration keys and match flags cross the console boundary.
    """
    return run_node(
        tenant,
        _LIST
        + """
const {readSignedJobs,sameCron,buildAddArgs,atFireMs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');
const signed=await readSignedJobs();
if(signed===null) throw Error('signature');
const desired=signed.filter(j=>j.enabled!==false &&
    (j.schedule?.kind!=='at'||atFireMs(j.schedule)>Date.now()));
if(desired.some(j=>!buildAddArgs(j))) throw Error('unmappable');
const removed=[];
const matches=desired.map(d=>{
    const found=jobs.filter(j=>j.declarationKey===d.declarationKey);
    const valid=found.length===1 && found[0].enabled!==false && sameCron(found[0],d);
    if(valid && CLEANUP){
        for(const legacy of jobs.filter(j=>!String(j.declarationKey||'').startsWith('nbhd:'))){
            if(legacy.enabled!==false && sameCron({...legacy,declarationKey:d.declarationKey},d)){
                oc(['cron','rm',legacy.id||legacy.jobId]);removed.push(legacy.id||legacy.jobId);
            }
        }
    }
    return {key:d.declarationKey,id:found[0]?.id||found[0]?.jobId||'',match:valid};
});
const keys=new Set(desired.map(d=>d.declarationKey));
const extras=jobs.filter(j=>String(j.declarationKey||'').startsWith('nbhd:')&&!keys.has(j.declarationKey));
const legacy=jobs.filter(j=>j.enabled!==false && !String(j.declarationKey||'').startsWith('nbhd:') &&
    !removed.includes(j.id||j.jobId)).map(j=>j.id||j.jobId);
return {matches,removed,legacy,extras:extras.map(j=>j.id||j.jobId),expected:desired.length};
""".replace("CLEANUP", "true" if cleanup else "false"),
    )


def config_observed(tenant) -> dict:
    return run_node(
        tenant,
        """
const file=fs.readFileSync(process.env.OPENCLAW_CONFIG_PATH,'utf8');
const digest=crypto.createHash('sha256').update(file).digest('hex');
const remote=JSON.parse(oc(['gateway','call','config.get','--json']));
// 9.4 hashes are opaque HMAC revision tokens; compare resolved vs applied,
// never compare a token with the raw SHA-256 of the source file.
if(remote.valid!==true || !remote.appliedConfigHash ||
    remote.appliedConfigHash!==remote.configRevisionHash) throw Error('config not applied');
JSON.parse(oc(['health','--json']));
return {sha256:digest,valid:true,appliedRevision:remote.appliedConfigHash};
""",
    )


def console_error_counts(tenant, *, since) -> dict:
    """Inspect recent console records locally; retain counts, never log lines.

    A saturated tail cannot establish a clean time window, so it fails closed.
    """
    from django.utils.dateparse import parse_datetime

    container, token = _connection(tenant)
    response = requests.get(
        container.log_stream_endpoint,
        headers={"Authorization": f"Bearer {token}"},
        params={"follow": "false", "output": "json", "tailLines": 300},
        timeout=30,
    )
    response.raise_for_status()
    rows = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    timestamps = [parse_datetime(row.get("TimeStamp", "")) for row in rows]
    if not rows or any(t is None for t in timestamps):
        raise OperatorError("Console log window unavailable")
    if len(rows) >= 300 and min(timestamps) > since:
        raise OperatorError("Console log tail does not cover verification window")
    patterns = {
        "fs_safe": r"FsSafeError|fs-safe.*(?:error|fail)",
        "sqlite": r"SQLite.*(?:error|fail|invalid|corrupt)",
        "config": r"(?:invalid|failed|error).*config|config.*(?:invalid|error|failed)|last.good.*rollback",
        "proxy": r"proxy_attribution_required|proxy.*(?:error|fail)",
        "signature": r"signature INVALID|cron-sync.*(?:REFUSED|failed|unmappable)",
    }
    recent = [str(row.get("Log", "")) for row, stamp in zip(rows, timestamps) if stamp >= since]
    counts = {key: sum(bool(re.search(pattern, line, re.I)) for line in recent) for key, pattern in patterns.items()}
    return {"since": since.isoformat(), "lines": len(recent), "errors": counts}


def capture_cron_declarations(tenant):
    """Transfer full declarations over authenticated file storage, never stdout."""
    import uuid

    from .azure_client import delete_workspace_file, download_workspace_file_binary
    from .cron_declarations import supported_declaration

    name = f"nbhd-cron-recovery-{uuid.uuid4().hex}.json"
    _queue_transfer_cleanup(tenant, name)
    path_js = (
        "require('node:path').join(require('node:path').dirname(process.env.OPENCLAW_CONFIG_PATH),"
        + json.dumps(name)
        + ")"
    )
    try:
        result = run_node(
            tenant,
            _LIST
            + f"const data=JSON.stringify(jobs);fs.writeFileSync({path_js},data,{{mode:0o600}});return {{sha256:crypto.createHash('sha256').update(data).digest('hex')}};",
        )
        data = download_workspace_file_binary(str(tenant.pk), name)
        import hashlib

        if not data or hashlib.sha256(data).hexdigest() != result["sha256"]:
            raise OperatorError("recovery_export_invalid")
        jobs = json.loads(data)
        if not isinstance(jobs, list) or any(not supported_declaration(j) for j in jobs):
            raise OperatorError("unsupported_cron")
        if any(not (j.get("id") or j.get("jobId")) for j in jobs):
            raise OperatorError("recovery_export_invalid")
        return [dict(j, id=j.get("id") or j["jobId"]) for j in jobs]
    finally:
        delete_workspace_file(str(tenant.pk), name)


def restore_crons(tenant, declarations):
    """Idempotent private-file restore, followed by full in-replica comparison."""
    import hmac
    import uuid
    from hashlib import sha256

    from apps.cron.gateway_client import get_gateway_token_for_tenant

    from .azure_client import _put_share_file, delete_workspace_file

    name = f"nbhd-cron-restore-{uuid.uuid4().hex}.json"
    _queue_transfer_cleanup(tenant, name)
    signed = json.dumps(declarations, sort_keys=True, separators=(",", ":"))
    key = get_gateway_token_for_tenant(tenant)
    if not key:
        raise OperatorError("recovery_key_missing")
    envelope = {"signed": signed, "sig": hmac.new(key.encode(), signed.encode(), sha256).hexdigest()}
    _put_share_file(str(tenant.pk), name, data=json.dumps(envelope).encode(), ensure_dirs=False)
    path_js = (
        "require('node:path').join(require('node:path').dirname(process.env.OPENCLAW_CONFIG_PATH),"
        + json.dumps(name)
        + ")"
    )
    try:
        return run_node(
            tenant,
            """
const lock=require('node:path').join(require('node:path').dirname(process.env.OPENCLAW_CONFIG_PATH),'.nbhd-cron-restore.lock');
if(fs.existsSync(lock) && Date.now()-fs.statSync(lock).mtimeMs>120000)fs.rmSync(lock,{force:true});
const fd=fs.openSync(lock,'wx',0o600);
try {
"""
            + _LIST
            + """
const {sameCron,buildAddArgs,applyCron}=await import('/opt/nbhd/nbhd-cron-sync.mjs');
const envelope=JSON.parse(fs.readFileSync(FILE,'utf8'));
const sig=crypto.createHmac('sha256',process.env.NBHD_INTERNAL_API_KEY).update(envelope.signed).digest('hex');
if(sig!==envelope.sig) throw Error('signature');
const desired=JSON.parse(envelope.signed);
const normalized=j=>({...j,declarationKey:'nbhd:recovery'});
// Other creators may have recovered the same declaration with a different ID.
// Include semantic/name conflicts, not just our IDs, in EVERY observation.
const candidates=(rows,d,key)=>rows.filter(j=>(j.id||j.jobId)===(d.id||d.jobId)||j.declarationKey===key||j.name===d.name||sameCron(normalized(j),normalized(d)));
const read=()=>{const doc=JSON.parse(oc(['cron','list','--all','--json']));const rows=Array.isArray(doc)?doc:doc.jobs;if(!Array.isArray(rows)||doc.hasMore===true||Number(doc.total||0)>rows.length||doc.nextOffset)throw Error('incomplete');return rows;};
for(const d of desired){
    const key=d.declarationKey || ('recovery:'+crypto.createHash('sha256').update(d.id||d.jobId).digest('hex'));
    let found=candidates(read(),d,key);
    if(found.length>1) throw Error('ambiguous');
    if(!found.length){
        if(d.enabled!==false && d.schedule?.kind==='at' && (d.schedule.atMs ?? Date.parse(d.schedule.at||''))<=Date.now()) throw Error('expired');
        const args=buildAddArgs(normalized(d));
        if(!args) throw Error('unsupported');
        args[args.indexOf('--declaration-key')+1]=key;
        if(d.enabled===false)args.push('--disabled');
        await applyCron({...d,declarationKey:key}, async args=>oc(args), args);
    }else{
        if(!sameCron(normalized(found[0]),normalized(d)))throw Error('conflict');
        if((found[0].enabled!==false)!==(d.enabled!==false))oc(['cron',d.enabled===false?'disable':'enable',found[0].id||found[0].jobId]);
    }
}
const observed=read();
const verified=desired.every(d=>{
    const key=d.declarationKey || ('recovery:'+crypto.createHash('sha256').update(d.id||d.jobId).digest('hex'));
    const found=candidates(observed,d,key);
    return found.length===1 && (found[0].enabled!==false)===(d.enabled!==false) && sameCron(normalized(found[0]),normalized(d));
});
return {verified,count:desired.length};
} finally {fs.closeSync(fd);fs.rmSync(lock,{force:true});}
""".replace("FILE", path_js),
        )
    finally:
        delete_workspace_file(str(tenant.pk), name)


def _queue_transfer_cleanup(tenant, name):
    """A delayed data-plane delete survives controller/replica termination."""
    if is_mock():
        return
    if not settings.QSTASH_TOKEN or not settings.API_BASE_URL:
        raise OperatorError("recovery_cleanup_queue_unavailable")
    from apps.cron.publish import publish_task

    publish_task("cleanup_cron_transfer", str(tenant.pk), name, delay_seconds=300)


def cleanup_cron_transfer_task(tenant_id, name):
    from .azure_client import delete_workspace_file

    if not re.fullmatch(r"nbhd-cron-(?:recovery|restore)-[0-9a-f]{32}\.json", name):
        raise ValueError("invalid_transfer_name")
    delete_workspace_file(str(tenant_id), name)
