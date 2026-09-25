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


def _comparison_adapter():
    """Bundle the same semantic comparator used by offline fixture tests."""
    digest = Path(__file__).with_name("migration_cron_digest.mjs").read_text()
    digest = digest.replace("import fs from 'node:fs';", "")
    digest = digest.replace(
        "JSON.parse(fs.readFileSync(new URL('./cron-normalization.json', import.meta.url)))",
        Path(__file__).with_name("cron-normalization.json").read_text().strip(),
    )
    digest = digest.replace(
        "JSON.parse(fs.readFileSync(new URL('./cron-proven-shapes.json', import.meta.url)))",
        Path(__file__).with_name("cron-proven-shapes.json").read_text().strip(),
    )
    comparator = (
        Path(__file__)
        .with_name("migration_cron_compare.mjs")
        .read_text()
        .replace(
            "import { normalizedDeclaration, stableJSON, provenShape, normalizedOptionals } from './migration_cron_digest.mjs';",
            "",
        )
    )
    return (digest + comparator).replace("export ", "")


def run_node(tenant, body: str, *, timeout: int = 90):
    """Execute a fixed JS adapter; only its explicit JSON result leaves the replica."""
    container, token = _connection(tenant)
    # Supply the migration comparator without changing the runtime image.
    if "readSignedJobs,sameCron" in body:
        adapter = _comparison_adapter()
        body = body.replace(
            "const {readSignedJobs,sameCron,buildAddArgs,atFireMs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');",
            "const {readSignedJobs}=await import('/opt/nbhd/nbhd-cron-sync.mjs');" + adapter,
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
const allJobs=Array.isArray(doc)?doc:doc.jobs;
if(!Array.isArray(allJobs)) throw Error('shape');
if(doc.hasMore===true || Number(doc.total||0)>allJobs.length ||
    (doc.nextOffset!=null && doc.nextOffset!==false)) throw Error('incomplete list');
// The real CLI reserves these namespaces and refuses removal. These monitor
// rows are recreated by the gateway itself, not by the signed cron writer.
const jobs=allJobs.filter(j=>!(['heartbeat:','skill-collection-review:'].some(prefix=>
    typeof j.agentId==='string' && j.agentId && j.declarationKey===prefix+j.agentId)));

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


def inspect_signed_crons(tenant, *, cleanup: bool = False, canonical_digests=None) -> dict:
    """Compare in-container desired jobs, remove only proven legacy duplicates.

    Comparison includes payloads INSIDE the replica via the image's tested adapter.
    Only IDs, declaration keys and match flags cross the console boundary.
    """
    private_expected = json.dumps(canonical_digests) if canonical_digests is not None else "null"
    return run_node(
        tenant,
        "const canonical="
        + private_expected
        + ";"
        + _LIST
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
    let valid=found.length===1 && found[0].enabled!==false && sameCron(found[0],d);
    if(canonical!==null) {
        const expected=canonical[d.declarationKey];
        valid=valid && !!expected && crypto.createHash('sha256').update(stableJSON(normalizedDeclaration(found[0],expected.pins))).digest('hex')===expected.digest;
    }
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


def preservation_inventory(tenant):
    """Read declaration structure; redact prose/destinations inside the replica."""
    return run_node(
        tenant,
        _LIST
        + r"""
return jobs.map(j=>{
    const copy=structuredClone(j);
    const deny=/"(command|commandArgv|command_argv|commandInput|commandCwd|commandEnv|script)"\s*:/i.test(JSON.stringify(j));
    for(const key of ['description','sessionKey','agentId']) if(copy[key]) copy[key]='present';
    if(copy.displayName) copy.displayName=copy.displayName===j.name?copy.name:'different';
    if(copy.payload) for(const key of ['message','text','event','model','thinking']) if(copy.payload[key]!=null) copy.payload[key]=String(copy.payload[key]).trim()?'present':'';
    if(copy.delivery) for(const key of ['to','accountId','threadId']) if(copy.delivery[key]) copy.delivery[key]='present';
    if(deny) copy.unsupportedWriterKey=true;
    return copy;
});
""",
    )
