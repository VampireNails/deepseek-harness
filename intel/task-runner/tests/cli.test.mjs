import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm,rename} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,delimiter} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

test('Linux CLI sends the live template with an explicit model patch and records timeout failure',
  {skip:process.platform!=='linux'},async t=>{
    const dir=await mkdtemp(join(tmpdir(),'intel-runner-test-'));t.after(()=>rm(dir,{recursive:true,force:true}));
    const bin=join(dir,'bin'),home=join(dir,'home');await mkdir(bin);await mkdir(home);
    await writeFile(join(bin,'dsh'),`#!/usr/bin/env node
import fs from 'node:fs';
const patch=process.argv[process.argv.indexOf('--patch')+1];
fs.writeFileSync(process.env.TEST_CAPTURE,JSON.stringify({patch:JSON.parse(fs.readFileSync(patch,'utf8')),profile:process.argv[process.argv.indexOf('--profile')+1],prompt:fs.readFileSync(0,'utf8')}));
if(process.env.TEST_HANG==='1')setInterval(()=>{},1000);
if(process.env.TEST_DESCENDANT==='1'){
  const {spawn}=await import('node:child_process');
  const child=spawn(process.execPath,['-e',"process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"],{stdio:'ignore'});
  fs.writeFileSync(process.env.TEST_PID,String(child.pid));setInterval(()=>{},1000);
}
`,{mode:0o700});
    const config=join(dir,'routes.json'),capture=join(dir,'capture.json');
    await writeFile(config,JSON.stringify({timeoutMs:1000,defaultRoute:'flash',routes:{flash:{provider:'deepseek-official',model:'deepseek-flash'}}}));
    const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
    const env={...process.env,PATH:bin+delimiter+process.env.PATH,DSH_HOME:home,INTEL_ROUTER_CONFIG:config,TEST_CAPTURE:capture};
    const result=spawnSync(process.execPath,[cli,'evolve_dreaming','stale'],{env,encoding:'utf8',timeout:10000});
    assert.equal(result.status,0,result.stderr);
    const sent=JSON.parse(await readFile(capture,'utf8'));
    assert.equal(sent.profile,'evolve');assert.ok(sent.prompt.includes('joblog'));assert.notEqual(sent.prompt.trim(),'stale');
    assert.deepEqual(sent.patch,[{id:'agent-default-model',config:{provider:'deepseek-official',model:'deepseek-flash'}}]);
    const records=await readFile(join(home,'intel-joblog','route-runs.jsonl'),'utf8');
    assert.equal(records.includes('stale'),false);assert.equal(records.includes('prompt'),false);
    const timeout=spawnSync(process.execPath,[cli,'custom','read only'],{env:{...env,TEST_HANG:'1'},encoding:'utf8',timeout:10000});
    assert.equal(timeout.status,124,timeout.stderr);
    const runs=JSON.parse(await readFile(join(home,'intel-joblog','job_runs.json'),'utf8'));
    assert.equal(runs.custom.status,'fail');assert.match(await readFile(join(home,'intel-joblog','job_alerts.md'),'utf8'),/exit 124/);
    const pidFile=join(dir,'child.pid');
    const group=spawnSync(process.execPath,[cli,'custom','read only'],{env:{...env,TEST_DESCENDANT:'1',TEST_PID:pidFile},encoding:'utf8',timeout:10000});
    assert.equal(group.status,124,group.stderr);
    const pid=Number(await readFile(pidFile,'utf8'));
    let dead=false;
    for(let i=0;i<30;i++){
      try{const stat=await readFile('/proc/'+pid+'/stat','utf8');if(stat.split(' ')[2]==='Z'){dead=true;break;}}
      catch(error){if(error.code==='ENOENT'){dead=true;break;}throw error;}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    assert.ok(dead,'timeout must terminate descendants that ignored SIGTERM');
    await rename(join(bin,'dsh'),join(bin,'dsh-disabled'));
    const failed=spawnSync(process.execPath,[cli,'evolve_dreaming'],{env:{...env,PATH:bin},encoding:'utf8',timeout:10000});
    assert.equal(failed.status,1,failed.stderr);
    const failedRuns=JSON.parse(await readFile(join(home,'intel-joblog','job_runs.json'),'utf8'));
    assert.equal(failedRuns.evolve_dreaming.status,'fail');
    assert.match(await readFile(join(home,'intel-joblog','job_alerts.md'),'utf8'),/ENOENT/);
  });
