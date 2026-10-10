import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {join,dirname} from 'node:path';
import {tmpdir} from 'node:os';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

test('private runtime channel rejects stale, malformed, partial and reset side-effect evidence',
  {skip:process.platform!=='linux'},async t=>{
    const root=await mkdtemp(join(tmpdir(),'routing-channel-'));t.after(()=>rm(root,{recursive:true,force:true}));
    const bin=join(root,'bin'),home=join(root,'home');await mkdir(bin);await mkdir(home);
    await writeFile(join(bin,'dsh'),`#!/usr/bin/env node
import {writeSync} from 'node:fs';
process.stdin.resume();
const row={version:1,runId:process.env.INTEL_RUN_ID,attempt:Number(process.env.INTEL_RUN_ATTEMPT),sessions:1,steps:1,requests:1,
  tools:0,outputs:0,retries:0,complete:true,streamOutput:false,provider:process.env.INTEL_ROUTE_PROVIDER,model:process.env.INTEL_ROUTE_MODEL,
  modelMatches:true,end:'error',failure:{code:'QUOTA',status:402}};
const emit=value=>writeSync(3,JSON.stringify(value)+'\\n');
switch(process.env.T06_CHANNEL_MODE){
case 'stale':row.runId='stale';emit(row);break;
case 'malformed':writeSync(3,'{bad\\n');break;
case 'partial':writeSync(3,JSON.stringify(row));break;
case 'oversized':emit({...row,extra:'x'.repeat(9000)});break;
case 'reset':emit({...row,complete:false,tools:1});emit(row);break;
default:emit(row);
}
process.exitCode=7;
`,{mode:0o700});
    const config=join(root,'routes.json');await writeFile(config,JSON.stringify({timeoutMs:1000,defaultRoute:'a',routes:{a:{provider:'p',model:'a'},b:{provider:'q',model:'b'}},
      tasks:{custom:{route:'a',fallback:'b',retrySafe:true}},fallback:{enabled:true,maxAttempts:2,totalTimeoutMs:2000}}));
    for(const mode of ['stale','malformed','partial','oversized','reset','valid']){
      const result=spawnSync(process.execPath,[fileURLToPath(new URL('../cli.mjs',import.meta.url)),'custom','private'],{encoding:'utf8',timeout:10000,
        env:{...process.env,PATH:bin+':'+dirname(process.execPath)+':/usr/bin:/bin',DSH_HOME:home,INTEL_ROUTER_CONFIG:config,INTEL_JOBLOG_SCOPE:'test',T06_CHANNEL_MODE:mode}});
      assert.equal(result.signal,null,result.stderr);assert.equal(result.status,7,result.stderr);
      const rows=(await readFile(join(home,'intel-joblog','route-runs.jsonl'),'utf8')).trim().split('\n').map(JSON.parse),finish=rows.at(-1);
      assert.equal(finish.kind,'finish');assert.equal(finish.attempts,mode==='valid'?2:1);
      assert.equal(finish.code,['reset','valid'].includes(mode)?'MODEL_QUOTA':'TASK_RESULT_UNKNOWN');
    }
  });
