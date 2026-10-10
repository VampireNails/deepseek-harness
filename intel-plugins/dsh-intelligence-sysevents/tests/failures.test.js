import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {emitEvent,listEvents} from '../src/store.js';
import * as fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import {spawnSync,spawn} from 'node:child_process';
import {once} from 'node:events';

function fixture(t){const dir=mkdtempSync(join(tmpdir(),'sysevents-failure-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));return dir;}
const entry={type:'task.failed',severity:'high',title:'任务执行失败',scope:'production',source:'runner',taskId:'custom',runId:'run-1',code:'TASK_EXIT_NONZERO',exitCode:1,exitCategory:'nonzero'};
test('structured failure identity deduplicates repeated reports and test scope has its own file',t=>{
  const dir=fixture(t),one=emitEvent(entry,dir),two=emitEvent(entry,dir);
  assert.deepEqual(two,one);assert.equal(listEvents({},dir).length,1);assert.equal(one.runId,entry.runId);
  const {id,ts,...projection}=one;
  assert.deepEqual(projection,JSON.parse(readFileSync(new URL('./fixtures/failure-event.expected.json',import.meta.url),'utf8')));
  const three=emitEvent({...entry,scope:'test'},dir);assert.notEqual(three.id,one.id);
  assert.equal(listEvents({},dir).length,1);assert.equal(listEvents({includeTests:true},dir).length,2);
  assert.throws(()=>emitEvent({...entry,code:'OTHER'},dir),e=>e.code==='SYSEVENTS_IDENTITY_CONFLICT');
});
test('broken event source cannot be read as empty or appended as success',t=>{
  const dir=fixture(t);writeFileSync(join(dir,'events.jsonl'),'{broken\n');
  assert.throws(()=>listEvents({},dir),e=>e.code==='SYSEVENTS_INVALID_DATA');
  assert.throws(()=>emitEvent(entry,dir),e=>e.code==='SYSEVENTS_INVALID_DATA');
  assert.equal(readFileSync(join(dir,'events.jsonl'),'utf8'),'{broken\n');
});
test('invalid scopes and raw diagnostic fields never become events',t=>{
  const dir=fixture(t);assert.throws(()=>emitEvent({...entry,scope:'guess'},dir));
  const value=emitEvent({...entry,prompt:'PRIVATE',token:'SECRET',path:'/private'},dir);
  assert.doesNotMatch(JSON.stringify(value),/PRIVATE|SECRET|\/private/);
  mkdirSync(join(dir,'events.test.jsonl'));
  assert.throws(()=>emitEvent({...entry,scope:'test'},dir),e=>e.code==='SYSEVENTS_READ_FAILED');
});
test('fsync/write faults reject success and notification retry never duplicates the task event',t=>{
  t.after(()=>{t.mock.restoreAll();syncBuiltinESMExports();});
  const dir=fixture(t);const flush=t.mock.method(fs.default,'fsyncSync',()=>{throw Object.assign(Error('private path'),{code:'EIO'});});syncBuiltinESMExports();
  assert.throws(()=>emitEvent(entry,dir),e=>e.code==='SYSEVENTS_WRITE_FAILED');
  assert.equal(listEvents({},dir).length,1);assert.throws(()=>emitEvent(entry,dir),e=>e.code==='SYSEVENTS_WRITE_FAILED');
  flush.mock.restore();syncBuiltinESMExports();assert.equal(emitEvent(entry,dir).runId,'run-1');assert.equal(listEvents({},dir).length,1);
  const write=t.mock.method(fs.default,'writeSync',()=>{throw Object.assign(Error('PRIVATE'),{code:'EIO'});});syncBuiltinESMExports();
  assert.throws(()=>emitEvent({...entry,runId:'run-2'},dir),e=>e.code==='SYSEVENTS_WRITE_FAILED');
  write.mock.restore();syncBuiltinESMExports();assert.equal(listEvents({},dir).length,1);
});
test('real Linux permission refusal is explicit and preserves the source',{skip:process.platform!=='linux'||process.getuid?.()!==0},t=>{
  const dir=fixture(t);fs.chmodSync(dir,0o755);emitEvent(entry,dir);const file=join(dir,'events.jsonl');fs.chmodSync(file,0o600);
  const script=`import {emitEvent,listEvents} from ${JSON.stringify(new URL('../src/store.js',import.meta.url).href)};
    for(const fn of [()=>listEvents({},process.env.EVENT_DIR),()=>emitEvent(${JSON.stringify(entry)},process.env.EVENT_DIR)]){try{fn();process.exit(2);}catch(e){console.log(e.code);}}`;
  const result=spawnSync(process.execPath,['--input-type=module','-e',script],{uid:65534,gid:65534,encoding:'utf8',env:{...process.env,EVENT_DIR:dir}});
  assert.equal(result.status,0,result.stderr);assert.equal(result.stdout.trim(),'SYSEVENTS_READ_FAILED\nSYSEVENTS_READ_FAILED');assert.equal(listEvents({},dir).length,1);
  const denied=fixture(t);fs.chmodSync(denied,0o555);
  const writeResult=spawnSync(process.execPath,['--input-type=module','-e',`import {emitEvent} from ${JSON.stringify(new URL('../src/store.js',import.meta.url).href)};try{emitEvent(${JSON.stringify(entry)},process.env.EVENT_DIR);process.exit(2);}catch(e){console.log(e.code);}`],{uid:65534,gid:65534,encoding:'utf8',env:{...process.env,EVENT_DIR:denied}});
  assert.equal(writeResult.status,0,writeResult.stderr);assert.match(writeResult.stdout,/SYSEVENTS_WRITE_FAILED/);fs.chmodSync(denied,0o700);
});
test('independent Linux writers deduplicate a shared structured identity',{skip:process.platform!=='linux'},async t=>{
  const dir=fixture(t),script=`import {emitEvent} from ${JSON.stringify(new URL('../src/store.js',import.meta.url).href)};emitEvent(${JSON.stringify(entry)},process.env.EVENT_DIR);`;
  const children=Array.from({length:4},()=>spawn(process.execPath,['--input-type=module','-e',script],{env:{...process.env,EVENT_DIR:dir},stdio:'ignore'}));
  const results=await Promise.all(children.map(child=>once(child,'close')));assert.ok(results.every(([code])=>code===0));assert.equal(listEvents({},dir).length,1);
});
test('oversized retained source refuses both read and write without dropping identity checks',t=>{
  const dir=fixture(t),file=join(dir,'events.jsonl');writeFileSync(file,'');fs.truncateSync(file,16*1024*1024+1);
  assert.throws(()=>listEvents({},dir),e=>e.code==='SYSEVENTS_SOURCE_TOO_LARGE');assert.throws(()=>emitEvent(entry,dir),e=>e.code==='SYSEVENTS_SOURCE_TOO_LARGE');assert.equal(fs.statSync(file).size,16*1024*1024+1);
});
