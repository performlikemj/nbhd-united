import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
const cli=args=>{const r=spawnSync('openclaw',args,{encoding:'utf8',env:{...process.env,NODE_OPTIONS:''},timeout:60000});return {args,exitCode:r.status,stdout:r.stdout,stderr:r.stderr};};
const before=cli(['cron','list','--all','--json']);
const jobs=JSON.parse(before.stdout).jobs;
const probes=[];
for(const row of jobs){
 probes.push({key:row.declarationKey,remove:cli(['cron','rm',row.id]),claim:cli(['cron','add','synthetic-claim','--declaration-key',row.declarationKey,'--cron','17 9 * * *','--message','Synthetic contract reminder','--no-deliver'])});
}
const after=cli(['cron','list','--all','--json']);
fs.writeFileSync('/tmp/contract/runtime-owned.json',JSON.stringify({before,probes,after},null,2)+'\n');
console.log(JSON.stringify(probes));
