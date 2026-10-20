import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {JobLog} from '../../../intel-plugins/dsh-intelligence-joblog/src/joblog.js';

test('CLI persists only fixed process diagnostics across audit, JobLog and events; missing receipts never fallback',
  {skip:process.platform!=='linux'},async t=>{
    const root=await mkdtemp(join(tmpdir(),'b3-diagnostics-'));t.after(()=>rm(root,{recursive:true,force:true}));
    const home=join(root,'home'),bin=join(root,'bin');await mkdir(home);await mkdir(bin);
    await writeFile(join(bin,'dsh'),`#!/usr/bin/env node
import {writeSync,writeFileSync} from 'node:fs';
process.stdin.resume();
if(process.env.DIAG_MODE==='short')process.stderr.write('PRIVATE token=SECRET\\u001b[31m');
if(process.env.DIAG_MODE==='long')process.stderr.write('x'.repeat(100000));
if(process.env.DIAG_MODE==='plugin')process.stderr.write('dsh: plugin tree failed to load PRIVATE\\n');
if(process.env.DIAG_MODE==='fd')writeSync(3,'{broken\\n');
if(process.env.DIAG_MODE==='effect')writeFileSync(process.env.DIAG_MARKER,'effect');
process.stdin.on('end',()=>process.exit(7));
`,{mode:0o700});
    const config=join(root,'routes.json');await writeFile(config,JSON.stringify({timeoutMs:2000,defaultRoute:'a',
      routes:{a:{provider:'p',model:'a'},b:{provider:'q',model:'b'}},tasks:{custom:{route:'a',fallback:'b',retrySafe:true}},
      fallback:{enabled:true,maxAttempts:2,totalTimeoutMs:4000}}));
    const cli=fileURLToPath(new URL('../cli.mjs',import.meta.url));
    for(const [mode,expected] of [['empty','stderrBytes=0'],['short','stderrBytes=25'],['long','stderrBytes=65536; stderrCapped=true'],
      ['plugin','stderrHint=plugin-startup'],['fd','receipt=invalid; receiptIssue=malformed-json'],['effect','receipt=missing']]){
      const run=spawnSync(process.execPath,[cli,'custom','PRIVATE_PROMPT'],{encoding:'utf8',timeout:15000,env:{...process.env,
        PATH:bin+':'+process.env.PATH,DSH_HOME:home,INTEL_ROUTER_CONFIG:config,DIAG_MODE:mode,DIAG_MARKER:join(root,'effect')}});
      assert.equal(run.status,7,run.stderr);
      const audit=(await readFile(join(home,'intel-joblog/route-runs.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
      const finish=audit.findLast(row=>row.kind==='finish');assert.equal(finish.attempts,1);assert.equal(finish.fallbackReason,'unsafe-or-unknown-result');
      assert.ok(finish.processDiagnostic.includes(expected),finish.processDiagnostic);assert.equal(finish.actualModel,undefined);
      const job=new JobLog(join(home,'intel-joblog')).readHistory().find(row=>row.runId===finish.runId);
      const event=(await readFile(join(home,'intel-system-events/events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse).find(row=>row.runId===finish.runId);
      assert.ok(job.detail.includes(expected),job.detail);assert.ok(event.detail.includes(expected),event.detail);
      assert.equal(event.diagnostic.code,mode==='fd'?'TASK_RECEIPT_INVALID':'TASK_RECEIPT_MISSING');
      assert.doesNotMatch(JSON.stringify(audit)+JSON.stringify(job)+JSON.stringify(event)+run.stderr,/PRIVATE|SECRET|\u001b/);
    }
    assert.equal(await readFile(join(root,'effect'),'utf8'),'effect');
  });
