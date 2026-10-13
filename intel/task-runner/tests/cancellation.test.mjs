import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {join,dirname} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
function killGroup(pid){try{process.kill(-pid,'SIGKILL');}catch(error){if(error.code!=='ESRCH')throw error;}}
async function active(pid){
  try{return (await readFile('/proc/'+pid+'/stat','utf8')).split(' ')[2]!=='Z';}
  catch(error){if(error.code==='ENOENT')return false;throw error;}
}
async function bounded(promise){
  let timer;
  try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('process wait timeout')),10000);})]);}
  finally{clearTimeout(timer);}
}

test('cancelling the lock-owning CLI stops its detached task and never reports a trapped signal as success',
  {skip:process.platform!=='linux'},async t=>{
    const root=await mkdtemp(join(tmpdir(),'runner-cancel-'));t.after(()=>rm(root,{recursive:true,force:true}));
    const home=join(root,'home'),bin=join(root,'bin'),config=join(root,'routes.json');
    await mkdir(home);await mkdir(bin);
    await writeFile(join(bin,'dsh'),`#!/usr/bin/env node
import {writeFileSync} from 'node:fs';
writeFileSync(process.env.CANCEL_PID_FILE,String(process.pid));
if(process.env.CANCEL_TRAP==='1')process.on('SIGTERM',()=>process.exit(0));
if(process.env.CANCEL_TRAP==='2')process.on('SIGTERM',()=>{});
process.stdout.write('ready\\n');process.stdin.resume();setInterval(()=>{},1000);
`,{mode:0o700});
    await writeFile(config,JSON.stringify({timeoutMs:30000,defaultRoute:'a',routes:{a:{provider:'p',model:'a'},b:{provider:'p',model:'b'}},
      tasks:{custom:{route:'a',fallback:'b'}},fallback:{enabled:true,maxAttempts:2,totalTimeoutMs:30000}}));
    for(const [cancelSignal,trap] of [['SIGTERM','0'],['SIGTERM','1'],['SIGTERM','2'],['SIGINT','0']]){
      const marker=join(root,'pid-'+cancelSignal+'-'+trap);
      const child=spawn(process.execPath,[cli,'custom','private'],{detached:true,env:{...process.env,DSH_HOME:home,
        INTEL_ROUTER_CONFIG:config,INTEL_JOBLOG_SCOPE:'test',CANCEL_PID_FILE:marker,CANCEL_TRAP:trap,
        PATH:bin+':'+dirname(process.execPath)+':/usr/bin:/bin'},stdio:['ignore','pipe','pipe']});
      const closed=once(child,'close');let stderr='',pid;
      child.stderr.on('data',part=>stderr+=part);
      t.after(async()=>{killGroup(child.pid);if(!pid){try{pid=Number(await readFile(marker,'utf8'));}catch(error){if(error.code!=='ENOENT')throw error;}}if(pid)killGroup(pid);await closed;});
      const ready=once(child.stdout,'data');
      await bounded(Promise.race([ready,closed.then(()=>{throw Error('task exited before readiness: '+stderr);})]));
      pid=Number(await readFile(marker,'utf8'));assert.ok(await active(pid));
      child.kill(cancelSignal);
      const [exit,signal]=await bounded(closed);
      for(let n=0;n<100&&await active(pid);n++)await delay(20);
      assert.equal(await active(pid),false,'detached task survives cancellation');
      assert.equal(signal,null,stderr);assert.equal(exit,1,stderr);
      const rows=(await readFile(join(home,'intel-joblog','route-runs.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
      const finish=rows.findLast(row=>row.kind==='finish');assert.equal(finish.code,'TASK_SIGNAL');assert.equal(finish.signal,cancelSignal);assert.equal(finish.attempts,1);
      const jobs=JSON.parse(await readFile(join(home,'intel-joblog','job_test_runs.json'),'utf8'));
      assert.equal(jobs.custom.status,'fail');assert.equal(jobs.custom.diagnostic.code,'TASK_SIGNAL');
    }
  });
