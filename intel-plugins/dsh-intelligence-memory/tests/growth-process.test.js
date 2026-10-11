import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fork} from 'node:child_process';
import {once} from 'node:events';
import {openStore} from '../src/store.js';

test('independent writers overlap under a held transaction then reuse one committed source identity',async t=>{
  const dir=mkdtempSync(join(tmpdir(),'memory-writer-barrier-'));
  openStore(dir).close(); const workers=[];
  t.after(async()=>{for(const {child,closed} of workers){if(child.exitCode===null)child.kill();await closed;}rmSync(dir,{recursive:true});});
  function start(){
    const child=fork(new URL('./fixtures/growth-worker.js',import.meta.url),[dir],{stdio:['ignore','ignore','pipe','ipc']});
    const closed=once(child,'close');workers.push({child,closed});return child;
  }
  const a=start(),aReady=once(a,'message'),b=start(),bReady=once(b,'message');
  assert.equal((await aReady)[0].state,'ready');assert.equal((await bReady)[0].state,'ready');
  const run=async(child,command)=>{const response=once(child,'message');child.send(command);return (await response)[0];};
  const held=await run(a,'hold');assert.equal(held.state,'holding');
  const busy=await run(b,'add');assert.equal(busy.state,'failed');assert.equal(busy.code,'ERR_SQLITE_ERROR');assert.equal(busy.errcode,5);
  assert.equal((await run(a,'commit')).state,'committed');
  const repeated=await run(b,'add');assert.equal(repeated.state,'added');assert.equal(repeated.result.id,held.result.id);assert.equal(repeated.result.deduplicated,true);assert.equal(repeated.result.capacity.rows,1);
  for(const {child,closed} of workers){child.send('close');const [code,signal]=await closed;assert.equal(code,0);assert.equal(signal,null);}
  const db=openStore(dir);try{assert.equal(db.prepare('SELECT count(*) AS n FROM memories').get().n,1);assert.equal(db.prepare('SELECT count(*) AS n FROM memories_fts').get().n,1);}finally{db.close();}
});
