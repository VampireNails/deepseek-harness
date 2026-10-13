import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync,symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {measureRuns} from '../measure.mjs';
const row=(kind,runId,extra={})=>({kind,runId,taskId:'evolve_dreaming',runDate:'2026-10-12',scope:'production',...extra});
test('daily evidence pairs exact run identities and separates failed, pending and reused runs',()=>{
 const result=measureRuns([row('start','a'),row('finish','a',{exit:1,code:'PROVIDER_UNKNOWN'}),row('start','b'),row('finish','b',{exit:0}),row('start','c'),row('start','d'),row('already-published','d',{publishedRunId:'b'}),row('finish','d',{exit:0,reused:true}),row('finish','orphan',{exit:0})],'2026-10-12');
 assert.deepEqual(result.counts,{completed:3,succeeded:1,failed:1,reused:1,pending:1,orphan:1,ambiguous:0,legacyUnpaired:0,quiet:0,skipped:0,control:0,explicitTestRows:0});
 assert.equal(result.runs.find(r=>r.runId==='a').code,'PROVIDER_UNKNOWN');
 assert.equal(result.runs.find(r=>r.runId==='d').publishedRunId,'b');
});
test('ambiguous records, incomplete legacy identity and explicit tests never inflate successes',()=>{
 const rows=[row('start','a'),row('finish','a',{exit:0}),row('finish','a',{exit:0}),row('start',undefined),row('finish',undefined,{exit:0}),row('start','test',{scope:'test'}),row('finish','test',{scope:'test',exit:0}),row('start','other',{runDate:'2026-10-11'}),row('finish','other',{runDate:'2026-10-11',exit:0})];
 const r=measureRuns(rows,'2026-10-12');assert.equal(r.counts.succeeded,0);assert.equal(r.counts.ambiguous,1);assert.equal(r.counts.legacyUnpaired,2);assert.equal(r.counts.explicitTestRows,2);
});
test('quiet observations preserve zero execution counts without inventing model runs or quality',()=>{
 const r=measureRuns([row('heartbeat-healthy',undefined,{taskId:'heartbeat',runDate:undefined,time:'2026-10-11T16:01:00Z',reason:'healthy',modelExecutions:0}),row('upkeep-no-signal',undefined,{taskId:'evolve_upkeep'}),row('upkeep-backoff',undefined,{taskId:'evolve_upkeep'}),row('start','a'),row('finish','a',{exit:65,code:'TASK_NO_VISIBLE_OUTPUT',processExit:0})],'2026-10-12');
 assert.equal(r.counts.quiet,3);assert.equal(r.zeroModelReceipts,1);assert.equal(r.counts.failed,1);assert.equal(r.runs[0].processExit,0);assert.equal(r.counts.succeeded,0);
});
test('dated circuit skips and control observations remain zero-model receipts rather than completed runs',()=>{
 const kinds=['heartbeat-circuit-open','heartbeat-circuit-unavailable','circuit-open','circuit-released','upkeep-recovered'];
 const rows=kinds.map(kind=>({kind,time:'2026-10-13T16:15:00.000Z',scope:'production',taskId:kind.startsWith('heartbeat')?'heartbeat':kind.startsWith('upkeep')?'memory_upkeep':'custom',modelExecutions:0}));
 const r=measureRuns(rows,'2026-10-14');
 assert.equal(r.undatedRows,0);assert.equal(r.zeroModelReceipts,5);assert.equal(r.counts.completed,0);assert.equal(r.counts.succeeded,0);
 assert.equal(r.counts.skipped,3);assert.equal(r.counts.control,2);assert.equal(r.counts.quiet,0);
 assert.equal(measureRuns(rows,'2026-10-13').zeroModelReceipts,0);
 const tests=measureRuns(rows.map(r=>({...r,scope:'test'})),'2026-10-14');assert.equal(tests.counts.explicitTestRows,5);assert.equal(tests.zeroModelReceipts,0);
 assert.equal(measureRuns([{...rows[0],kind:'unknown-kind'}],'2026-10-14').undatedRows,1);
 assert.equal(measureRuns([{...rows[4],modelExecutions:undefined}],'2026-10-14').zeroModelReceipts,0);
 for(const extra of [{scope:'invalid'},{modelExecutions:1},{taskId:'other',kind:'heartbeat-circuit-open'},{taskId:'other',kind:'upkeep-recovered'}])
  assert.throws(()=>measureRuns([{...rows[0],...extra}],'2026-10-14'));
});
test('invalid exits, inconsistent reuse and invalid scope cannot be counted as successful runs',()=>{
 for(const extra of [{exit:'0'},{exit:-1},{exit:256},{exit:0,processExit:1},{exit:0,reused:'true'},{exit:0,reused:true},{exit:0,scope:'invalid'}]){
  const r=measureRuns([row('start','a'),row('finish','a',extra)],'2026-10-12');assert.equal(r.counts.succeeded,0);assert.equal(r.counts.reused,0);assert.equal(r.counts.ambiguous,1);
 }
 assert.throws(()=>measureRuns([],'2026-02-30'));
 assert.throws(()=>measureRuns([row('start','a',{runDate:'2026-02-30'})],'2026-10-12'));
 assert.throws(()=>measureRuns([row('start',123)],'2026-10-12'));
 assert.throws(()=>measureRuns([row('heartbeat-healthy',undefined,{modelExecutions:0})],'2026-10-12'));
});
test('real read-only CLI reports exact identities and safely rejects corrupt, partial, oversized or symlink input',()=>{
 const dir=mkdtempSync(join(tmpdir(),'owned-run-measure-'));
 const file=join(dir,'audit.jsonl'),cli=fileURLToPath(new URL('../measure.mjs',import.meta.url));
 const call=p=>spawnSync(process.execPath,[cli,p,'2026-10-12'],{encoding:'utf8',timeout:10000});
 try{
  writeFileSync(file,[row('start','a'),row('finish','a',{exit:0})].map(r=>JSON.stringify(r)+'\n').join(''));
  const before=call(file);assert.equal(before.status,0);assert.equal(JSON.parse(before.stdout).counts.succeeded,1);
  for(const value of ['PRIVATE_INVALID\n',JSON.stringify(row('start','a')),' '.repeat(16*1024*1024+1),Buffer.from([0xff,0x0a])]){
   writeFileSync(file,value);const r=call(file);assert.notEqual(r.status,0);assert.equal(r.stdout,'');assert.ok(!r.stderr.includes('PRIVATE'));
  }
  assert.notEqual(call(dir).status,0);
  if(process.platform!=='win32'){
   const link=join(dir,'link');symlinkSync(file,link);assert.notEqual(call(link).status,0);
   const fifo=join(dir,'fifo');assert.equal(spawnSync('mkfifo',[fifo]).status,0);const r=call(fifo);assert.notEqual(r.status,null);assert.notEqual(r.status,0);
  }
 }finally{rmSync(dir,{recursive:true,force:true});}
});
test('output omits arbitrary diagnostic bodies and rejects invalid date or records',()=>{
 const r=measureRuns([row('start','a',{prompt:'PRIVATE'}),row('finish','a',{exit:1,code:'bad PRIVATE',stderr:'PRIVATE'})],'2026-10-12');
 assert.ok(!JSON.stringify(r).includes('PRIVATE'));assert.equal(r.runs[0].code,'UNKNOWN');
 assert.throws(()=>measureRuns([], 'not-date'));assert.throws(()=>measureRuns([null],'2026-10-12'));
});
