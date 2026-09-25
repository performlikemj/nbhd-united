import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
async function main() {
const metadata=JSON.parse(fs.readFileSync('/tmp/contract/input-metadata.json'));
const writerHash=createHash('sha256').update(fs.readFileSync('/opt/nbhd/nbhd-cron-sync.mjs')).digest('hex');
if(writerHash!==metadata.sources['runtime/openclaw/nbhd-cron-sync.mjs']) throw Error('Image writer differs from unchanged repository writer');
const inputs=JSON.parse(fs.readFileSync('/tmp/contract/inputs.json'));
const {buildAddArgs,sameCron}=await import('/opt/nbhd/nbhd-cron-sync.mjs');
function cli(args) {
 const r=spawnSync('openclaw',args,{env:{...process.env,NODE_OPTIONS:''},encoding:'utf8',timeout:60000,maxBuffer:8*1024*1024});
 return {args,exitCode:r.status,stdout:r.stdout,stderr:r.stderr};
}
for(const entry of inputs) {
 process.env.NBHD_CRONS_FILE=`/tmp/contract/${entry.case}.signed.json`;
 const {readSignedJobs,reconcileOnce}=await import(`/opt/nbhd/nbhd-cron-sync.mjs?case=${entry.case}`);
 const signedJobs=await readSignedJobs();
 if(!signedJobs || signedJobs.length!==Number(entry.selected)) throw Error('signature/selection');
 const argv=buildAddArgs(entry.selected?signedJobs[0]:entry.declaration);
 const add=cli(argv);
 const list=cli(['cron','list','--all','--json']);
 if(list.exitCode!==0) throw Error('list_failed');
 const listJSON=JSON.parse(list.stdout);
 const row=listJSON.jobs.find(j=>j.declarationKey===entry.declaration.declarationKey);
 const stability=[];
 if(row && entry.selected) {
   for(let poll=0;poll<2;poll++) {
     const oldLog=console.log, oldWarn=console.warn, oldError=console.error;
     let reconciliation;
     try { console.log=console.warn=console.error=()=>{}; reconciliation=await reconcileOnce(); }
     finally { console.log=oldLog; console.warn=oldWarn; console.error=oldError; }
     const after=cli(['cron','list','--all','--json']);
     stability.push({reconciliation,list:after});
   }
 }
 const fixture={...entry,signedJobs,stability,writerArgv:argv,add,list:{...list,json:listJSON},writerSameCron:row?sameCron(row,entry.declaration):null};
 fs.writeFileSync(`/tmp/contract/${entry.case}.json`,JSON.stringify(fixture,null,2)+'\n');
 console.log(JSON.stringify({case:entry.case,status:add.exitCode,reason:row?'captured':'cli_rejected'}));
 if(row) {const latest=JSON.parse(cli(['cron','list','--all','--json']).stdout).jobs.find(j=>j.declarationKey===row.declarationKey); const removed=cli(['cron','rm',latest.id]);if(removed.exitCode!==0) throw Error('cleanup');}
}

fs.writeFileSync('/tmp/contract/runtime-metadata.json', JSON.stringify({
 ...metadata, writerHash, version:cli(['--version']),
 isolation:'Docker --network none, no ports or mounts; cron.enabled=false; plugins.enabled=false; throwaway token',
},null,2)+'\n');

}
await main().catch(()=>{
 console.error(JSON.stringify({reason:"capture_failed"}));
 process.exitCode=1;
});
