import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync,chmodSync,statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {JobLog} from '../src/joblog.js';
import {DatabaseSync} from 'node:sqlite';
import {spawn,spawnSync} from 'node:child_process';
import {persistRun} from '../src/history.js';

function fixture(t,options){
  const dir=mkdtempSync(join(tmpdir(),'joblog-history-'));
  t.after(()=>rmSync(dir,{recursive:true,force:true}));
  return new JobLog(dir,options);
}

test('fail fail ok keeps three run identities and duplicate completion counts once',t=>{
  const log=fixture(t);
  for(const [runId,status] of [['r1','fail'],['r2','fail'],['r3','ok']])
    log._recordRun('task',status,'exit '+(status==='ok'?0:1),{runId});
  log._recordRun('task','ok','exit 0',{runId:'r3'});
  const rows=log.readHistory();
  assert.deepEqual(rows.map(r=>r.status),['ok','fail','fail']);
  assert.deepEqual(rows.map(r=>r.runId),['r3','r2','r1']);
  assert.equal(log.readRuns().task.status,'ok');
});

test('an invalid latest file blocks completion without replacing the original bytes',t=>{
  const log=fixture(t);writeFileSync(log.runsFile,'{');
  assert.throws(()=>log._recordRun('task','ok','done'),{code:'JOBLOG_INVALID_DATA'});
  assert.equal(readFileSync(log.runsFile,'utf8'),'{');
});

test('partial projection failure is observable and does not turn a completed task into failure',async t=>{
  const log=fixture(t);mkdirSync(log.runsFile);
  await assert.rejects(log.guarded('task',async()=> 'private prompt'),{code:'JOBLOG_READ_FAILED'});
});

test('bounded history keeps latest per task, segregates scopes and preserves overwritten legacy snapshot',t=>{
  const log=fixture(t,{historyLimit:2});
  const old=JSON.stringify({same:{last:'old local date',status:'fail',detail:'legacy failure'}});
  writeFileSync(log.runsFile,old);
  const testing=new JobLog(log.dir,{scope:'test',historyLimit:2});
  for(let n=0;n<5;n++)log._recordRun(n===0?'other':'same',n===0?'fail':'ok','exit '+n,{runId:'p'+n});
  testing._recordRun('same','fail','test failure',{runId:'t1'});
  assert.deepEqual(log.readHistory().map(r=>r.runId),['p4','p3']);
  const all=log.readHistory({includeTests:true});
  assert.equal(all.filter(r=>r.scope==='unclassified').length,1);
  assert.equal(all.find(r=>r.scope==='unclassified').last,'old local date');
  assert.equal(all.find(r=>r.scope==='test').runId,'t1');
  assert.equal(log.readRuns().other.status,'fail');
  assert.ok(statSync(log.historyFile).size<1024*1024);
});

test('older duplicate completion cannot roll back a newer latest and conflicting identity fails',t=>{
  const log=fixture(t);
  log._recordRun('same','fail','failure',{runId:'old'});
  log._recordRun('same','ok','recovered',{runId:'new'});
  log._recordRun('same','fail','failure',{runId:'old'});
  assert.equal(log.readRuns().same.runId,'new');
  assert.throws(()=>log._recordRun('same','ok','changed',{runId:'old'}),{code:'JOBLOG_IDENTITY_COLLISION'});
  assert.equal(log.readHistory().length,2);
});

test('untouched legacy entries keep explicit unclassified source in history without rewriting bytes',t=>{
  const log=fixture(t),raw=JSON.stringify({old:{last:'old date',status:'fail'}});writeFileSync(log.runsFile,raw);
  assert.deepEqual(log.readHistory(),[]);assert.equal(log.readHistory({includeTests:true})[0].scope,'unclassified');
  assert.equal(log.readHistory({includeTests:true})[0].detail,'');
  assert.equal(readFileSync(log.runsFile,'utf8'),raw);
  log._recordRun('new','ok','done');const rows=log.readHistory({includeTests:true});assert.equal(rows.filter(r=>r.id==='old').length,1);
});

test('detail and summary are bounded, credential fields redacted and arbitrary guarded bodies never stored',async t=>{
  const log=fixture(t);log._recordRun('task','fail','locate exit 124\n'+'x'.repeat(4000)+'\napi_key=SECRET', {runId:'bounded'});
  const row=log.readHistory()[0];assert.equal(row.detail.length,2048);assert.equal(row.summary.length,200);assert.equal(row.detailTruncated,true);
  log._recordRun('task','ok','Bearer abcdef\nprompt=PRIVATE BODY\npassword=hidden',{runId:'redacted'});
  assert.doesNotMatch(log.readHistory()[0].detail,/abcdef|PRIVATE BODY|hidden/);
  assert.equal(await log.guarded('safe',async()=> 'SENSITIVE BODY'),'SENSITIVE BODY');
  const original=Object.assign(Error('secret body'),{code:'EFAIL'});
  await assert.rejects(log.guarded('safe',async()=>{throw original;}),e=>e===original);
  assert.doesNotMatch(readFileSync(log.logFile,'utf8')+readFileSync(log.runsFile,'utf8')+readFileSync(log.alertsFile,'utf8'),/SENSITIVE BODY|secret body/);
});

test('log and alert write faults reject, and a failed task retains its original error alongside storage code',async t=>{
  const log=fixture(t);mkdirSync(log.logFile);
  let called=false;await assert.rejects(log.guarded('task',async()=>{called=true;}),{code:'JOBLOG_WRITE_FAILED'});assert.equal(called,false);
  rmSync(log.logFile,{recursive:true});mkdirSync(log.alertsFile);
  const original=Error('private');
  await assert.rejects(log.guarded('task',async()=>{throw original;}),e=>e.code==='JOBLOG_PERSISTENCE_FAILED'&&e.taskError===original&&e.operation==='alerts');
  assert.equal(log.readHistory()[0].status,'fail');
});

test('corrupt history never falls back to a successful latest snapshot',t=>{
  const log=fixture(t);log._recordRun('task','ok','done');
  writeFileSync(log.historyFile,'damaged');
  assert.throws(()=>log.readHistory(),{code:'JOBLOG_READ_FAILED'});
  assert.throws(()=>log._recordRun('task','ok','again'),{code:'JOBLOG_WRITE_FAILED'});
});

test('a compatibility projection failure reports partial persistence while the committed completion remains authoritative',t=>{
  const log=fixture(t);log._recordRun('real','fail','retained',{runId:'real'});
  const row={id:'completed',runId:'completed',status:'ok',scope:'production',last:new Date().toISOString(),finishedAt:new Date().toISOString(),detail:'done',summary:'done',detailTruncated:false};
  assert.throws(()=>persistRun(log.historyFile,row,200,()=>log._readRunFile(log.runsFile),runs=>{
    throw Object.assign(Error('injected'),{code:'EIO'});
  }),e=>e.code==='JOBLOG_WRITE_FAILED'&&e.committed===true);
  assert.equal(log.readRuns().completed.status,'ok');assert.deepEqual(log.readHistory().map(r=>r.runId),['completed','real']);
  assert.equal(JSON.parse(readFileSync(log.runsFile,'utf8')).completed,undefined);
});

test('a SQLite commit-path failure rolls back without advancing the compatibility file',t=>{
  const log=fixture(t);log._recordRun('real','fail','retained',{runId:'real'});const before=readFileSync(log.runsFile,'utf8');
  const db=new DatabaseSync(log.historyFile);db.exec("CREATE TRIGGER reject_completion BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT,'injected'); END");db.close();
  assert.throws(()=>log._recordRun('blocked','ok','done'),{code:'JOBLOG_WRITE_FAILED'});
  assert.equal(readFileSync(log.runsFile,'utf8'),before);assert.equal(log.readRuns().blocked,undefined);
  assert.deepEqual(log.readHistory().map(r=>r.runId),['real']);
});

test('concurrent Linux processes wait for the same database transaction and keep every completion',
  {skip:process.platform!=='linux'},async t=>{
  const log=fixture(t);log._recordRun('seed','ok','seed',{runId:'seed'});
  const db=new DatabaseSync(log.historyFile);db.exec('BEGIN IMMEDIATE');
  const children=[],closed=[];
  t.after(async()=>{if(db.isOpen)db.close();for(const child of children)if(child.exitCode===null)child.kill();await Promise.all(closed);});
  const module=new URL('../src/joblog.js',import.meta.url).href;
  const source=`import {JobLog} from ${JSON.stringify(module)};const log=new JobLog(process.argv[1]);process.send('ready');process.once('message',()=>{process.send('attempt');log._recordRun(process.argv[2],'ok','done',{runId:process.argv[2]});process.disconnect();});`;
  function message(child,kind){return new Promise((resolve,reject)=>{const onMessage=value=>{if(value===kind){child.off('message',onMessage);resolve();}};child.on('message',onMessage);child.once('error',reject);});}
  let completed=0;
  for(const id of ['a','b']){
    const child=spawn(process.execPath,['--input-type=module','-e',source,log.dir,id],{stdio:['ignore','ignore','pipe','ipc']});children.push(child);
    let stderr='';child.stderr.on('data',b=>stderr+=b);
    closed.push(new Promise(resolve=>child.once('close',(code,signal)=>{completed++;resolve({code,signal,stderr});})));
  }
  await Promise.all(children.map(child=>message(child,'ready')));
  const attempts=children.map(child=>message(child,'attempt'));for(const child of children)child.send('go');
  await Promise.all(attempts);assert.equal(completed,0);db.exec('COMMIT');db.close();
  for(const result of await Promise.all(closed))assert.equal(result.code,0,result.stderr);
  assert.deepEqual(new Set(log.readHistory().map(r=>r.runId)),new Set(['seed','a','b']));
  assert.ok(JSON.parse(readFileSync(log.runsFile,'utf8')).a);assert.ok(JSON.parse(readFileSync(log.runsFile,'utf8')).b);
});

test('actual Linux permission failures expose only safe storage codes', {skip:process.platform!=='linux'||process.getuid()!==0},t=>{
  const log=fixture(t);chmodSync(log.dir,0o777);
  // The unprivileged child must load owned code even when the checkout is below /root.
  const code=join(log.dir,'code'),sourceDir=join(code,'intel-plugins','dsh-intelligence-joblog','src'),shared=join(code,'intel-plugins','shared');
  mkdirSync(sourceDir,{recursive:true});mkdirSync(shared,{recursive:true});
  for(const name of ['joblog.js','history.js'])writeFileSync(join(sourceDir,name),readFileSync(new URL('../src/'+name,import.meta.url)),{mode:0o644});
  writeFileSync(join(shared,'persistence.js'),readFileSync(new URL('../../shared/persistence.js',import.meta.url)),{mode:0o644});
  writeFileSync(join(code,'package.json'),' {"type":"module"}',{mode:0o644});
  writeFileSync(log.logFile,'old',{mode:0o400});writeFileSync(log.alertsFile,'old',{mode:0o400});
  const module='file://'+join(sourceDir,'joblog.js');
  const source=`import {JobLog} from ${JSON.stringify(module)};const log=new JobLog(process.argv[1]);for(const f of [()=>log._log('line'),()=>log._alert('task','detail')]){try{f();process.exitCode=2;}catch(e){if(e.code!=='JOBLOG_WRITE_FAILED'||e.errno!=='EACCES'||e.message.includes(process.argv[1]))process.exitCode=3;}}`;
  const result=spawnSync(process.execPath,['--input-type=module','-e',source,log.dir],{uid:65534,gid:65534,encoding:'utf8',timeout:10000});
  assert.equal(result.signal,null);assert.equal(result.status,0,result.stderr);
});
