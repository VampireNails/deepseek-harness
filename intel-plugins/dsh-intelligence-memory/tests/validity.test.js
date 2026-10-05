import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { openStore } from '../src/store.js';
import { FtsRetriever } from '../src/retriever.js';
import { inspectMemoryIndex } from '../src/maintenance.js';

function fixture(t) {
  const directory=mkdtempSync(join(tmpdir(),'memory-validity-'));
  let retriever=new FtsRetriever(openStore(directory));
  t.after(()=>{retriever.close();rmSync(directory,{recursive:true,force:true});});
  const rows=()=>retriever.db.prepare('SELECT * FROM memories ORDER BY id').all().map(row=>({...row}));
  return {directory,rows,get retriever(){return retriever;},reopen(){retriever.close();retriever=new FtsRetriever(openStore(directory));}};
}
const command=(id,action,revision,reason='anonymous correction')=>({id,action,expectedRevision:revision,reason,mutationId:randomUUID()});
const actor={source:'paired-client'};

test('a prepared store missing its marker cannot become a legacy store or reenable an invalidated original',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous marker failure'});
  r.change(command(id,'invalidate',0),actor);r.db.exec('DROP TABLE memory_validity_schema');
  const rows=f.rows(),audit=r.db.prepare('SELECT * FROM memory_audit').all(),fts=r.db.prepare('SELECT * FROM memories_fts').all();
  assert.throws(()=>new FtsRetriever(r.db),error=>error.code==='MEMORY_SCHEMA_INVALID');
  assert.throws(()=>{const unexpected=openStore(f.directory);unexpected.close();},error=>error.code==='MEMORY_SCHEMA_INVALID');
  assert.throws(()=>inspectMemoryIndex(r.db),error=>error.code==='MEMORY_SCHEMA_INVALID');
  assert.deepEqual(f.rows(),rows);assert.deepEqual(r.db.prepare('SELECT * FROM memory_audit').all(),audit);
  assert.deepEqual(r.db.prepare('SELECT * FROM memories_fts').all(),fts);
});

test('successful validity change participates in an outer transaction and outer rollback restores its index and audit',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous outer success'});
  r.db.exec('BEGIN');r.change(command(id,'invalidate',0),actor);
  assert.equal(r.get(id),null);assert.equal(r.history(id).length,1);
  r.db.exec('ROLLBACK');assert.equal(r.describe(id).revision,0);assert.equal(r.history(id).length,0);
  assert.deepEqual(r.search('outer').map(row=>row.id),[id]);
});

test('failed validity change preserves caller writes and leaves the outer transaction usable',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous outer failure'});
  r.db.exec("CREATE TABLE caller_work(value TEXT); CREATE TRIGGER fail_outer_audit BEFORE INSERT ON memory_audit BEGIN SELECT RAISE(ABORT,'fixture'); END; BEGIN; INSERT INTO caller_work VALUES('owned caller work');");
  assert.throws(()=>r.change(command(id,'invalidate',0),actor));
  r.db.exec('COMMIT');assert.equal(r.db.prepare('SELECT value FROM caller_work').get().value,'owned caller work');
  assert.equal(r.describe(id).revision,0);assert.equal(r.history(id).length,0);
  assert.deepEqual(r.search('outer').map(row=>row.id),[id]);
});

test('invalidation removes default retrieval while preserving the original row and durable audit',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous bicycle fact',kind:'fact',sessionId:'owned-session'}),before=f.rows();
  const result=r.change(command(id,'invalidate',0),actor);
  assert.equal(result.record.active,false);assert.equal(result.record.revision,1);
  assert.equal(r.get(id),null);assert.deepEqual(r.search('bicycle'),[]);assert.deepEqual(f.rows(),before);
  assert.equal(r.describe(id).text,'anonymous bicycle fact');assert.equal(r.history(id).length,1);
  f.reopen();assert.equal(f.retriever.get(id),null);assert.deepEqual(f.retriever.search('bicycle'),[]);
  assert.equal(f.retriever.describe(id).revision,1);assert.deepEqual(f.rows(),before);
});

test('restore reindexes original text and transport replay never repeats an audit or revision',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous restore marker'}),before=f.rows();
  const invalidate=command(id,'invalidate',0);const first=r.change(invalidate,actor);
  assert.deepEqual(r.change(invalidate,actor),first);assert.equal(r.history(id).length,1);
  const restore=command(id,'restore',1,'anonymous restore reason');const restored=r.change(restore,actor);
  assert.equal(restored.record.active,true);assert.equal(restored.record.revision,2);
  assert.deepEqual(r.search('restore').map(row=>row.id),[id]);assert.deepEqual(f.rows(),before);
  assert.deepEqual(r.change(invalidate,actor),first,'A late retry reports that operation, without re-invalidating after restore');
  assert.equal(r.describe(id).active,true);assert.equal(r.describe(id).revision,2);assert.equal(r.history(id).length,2);
  assert.deepEqual(r.change(restore,actor),restored);assert.equal(r.history(id).length,2);
});

test('stale state and reused IDs with different requests leave all memory and index data unchanged',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous conflict marker'}),change=command(id,'invalidate',0);
  r.change(change,actor);const before=JSON.stringify({rows:f.rows(),state:r.describe(id),audit:r.history(id)});
  assert.throws(()=>r.change(command(id,'restore',0),actor),error=>error.code==='MEMORY_REVISION_CONFLICT');
  assert.throws(()=>r.change({...change,reason:'different request'},actor),error=>error.code==='MEMORY_MUTATION_ID_REUSED');
  assert.equal(JSON.stringify({rows:f.rows(),state:r.describe(id),audit:r.history(id)}),before);
  assert.deepEqual(r.search('conflict'),[]);
});

test('audit insertion failure rolls back both state and FTS mutation',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous atomic marker'}),before=f.rows();
  r.db.exec("CREATE TRIGGER deny_audit BEFORE INSERT ON memory_audit BEGIN SELECT RAISE(ABORT,'controlled audit failure'); END");
  assert.throws(()=>r.change(command(id,'invalidate',0),actor));
  assert.equal(r.describe(id).active,true);assert.equal(r.describe(id).revision,0);assert.deepEqual(r.history(id),[]);
  assert.deepEqual(r.search('atomic').map(row=>row.id),[id]);assert.deepEqual(f.rows(),before);
});

test('untrusted IDs, actions, reasons, actor and pre-cancellation cannot mutate memory',t=>{
  const f=fixture(t),r=f.retriever,{id}=r.add({text:'anonymous validation marker'}),valid=command(id,'invalidate',0);
  for(const change of [{...valid,id:1.5},{...valid,id:-1},{...valid,action:'delete'},{...valid,reason:' '},{...valid,reason:'a'.repeat(2049)},{...valid,expectedRevision:-1},{...valid,mutationId:'not-a-request-id'}])assert.throws(()=>r.change(change,actor),error=>error.code==='MEMORY_INVALID_ARGUMENT');
  assert.throws(()=>r.change(valid,{source:'admin'}),error=>error.code==='MEMORY_INVALID_ACTOR');
  const signal=AbortSignal.abort();assert.throws(()=>r.change(valid,{...actor,signal}),error=>error.code==='MEMORY_CANCELLED');
  assert.equal(r.describe(id).revision,0);assert.deepEqual(r.history(id),[]);assert.deepEqual(r.search('validation').map(row=>row.id),[id]);
});

test('bounded active and invalidated lists keep their states distinct',t=>{
  const f=fixture(t),r=f.retriever,a=r.add({text:'anonymous active list'}).id,b=r.add({text:'anonymous invalidated list'}).id;
  r.change(command(b,'invalidate',0),actor);
  assert.deepEqual(r.list({mode:'active',limit:30}).items.map(row=>row.id),[a]);
  assert.deepEqual(r.list({mode:'invalidated',limit:30}).items.map(row=>row.id),[b]);
  assert.equal(r.list({mode:'all',limit:1}).items.length,1);assert.equal(r.list({mode:'all',query:'invalidated',limit:30}).items[0].active,false);
});
