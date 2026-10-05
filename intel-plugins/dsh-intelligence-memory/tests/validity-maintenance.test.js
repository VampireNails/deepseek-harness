import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {mkdtempSync,rmSync,existsSync,readFileSync} from 'node:fs';import {join} from 'node:path';import {tmpdir} from 'node:os';import {randomUUID} from 'node:crypto';
import {openStore} from '../src/store.js';import {FtsRetriever,spaceCjk} from '../src/retriever.js';import * as maintenance from '../src/maintenance.js';

function fixture(t){const directory=mkdtempSync(join(tmpdir(),'memory-validity-maintenance-'));t.after(()=>rmSync(directory,{recursive:true,force:true}));return {directory,path:join(directory,'memory.db'),backup:join(directory,'before-validity.db')};}
function originals(db){return db.prepare('SELECT * FROM memories ORDER BY id').all().map(row=>({...row}));}
function legacy(path){const db=new DatabaseSync(path);db.exec("CREATE TABLE memories(id INTEGER PRIMARY KEY,text TEXT NOT NULL,kind TEXT NOT NULL,created_at INTEGER NOT NULL,session_id TEXT); CREATE VIRTUAL TABLE memories_fts USING fts5(text);");db.prepare('INSERT INTO memories VALUES(?,?,?,?,?)').run(7,'anonymous legacy bicycle','fact',1000,'owned-legacy');db.prepare('INSERT INTO memories_fts(rowid,text) VALUES(?,?)').run(7,spaceCjk('anonymous legacy bicycle'));db.close();}

test('index repair removes stale inactive FTS without reviving or deleting invalidated originals',async t=>{
 const f=fixture(t);const r=new FtsRetriever(openStore(f.directory));const active=r.add({text:'anonymous valid bicycle'}).id,inactive=r.add({text:'anonymous invalidated bicycle'}).id;
 r.change({id:inactive,action:'invalidate',expectedRevision:0,reason:'controlled correction',mutationId:randomUUID()},{source:'paired-client'});
 const before=originals(r.db),audit=r.history(inactive);r.db.prepare('DELETE FROM memories_fts WHERE rowid=?').run(active);r.db.prepare('INSERT INTO memories_fts(rowid,text) VALUES(?,?)').run(inactive,spaceCjk('anonymous invalidated bicycle'));r.close();
 const report=await maintenance.repairMemoryIndex({databasePath:f.path,backupPath:f.backup});
 const reopened=new FtsRetriever(openStore(f.directory));try{assert.deepEqual(reopened.db.prepare('SELECT rowid FROM memories_fts ORDER BY rowid').all().map(row=>row.rowid),[active],'Maintenance must not index inactive originals again');assert.equal(report.after.inactiveMemoryRows,1);assert.equal(report.after.inactiveFtsRows,0);assert.equal(report.after.missingFtsRows,0);assert.deepEqual(reopened.search('bicycle').map(row=>row.id),[active]);assert.equal(reopened.get(inactive),null);assert.deepEqual(originals(reopened.db),before);assert.deepEqual(reopened.history(inactive),audit);}finally{reopened.close();}
});

test('legacy opening stays read compatible and preparation backs up before adding validity schema',async t=>{
 const f=fixture(t);legacy(f.path);const bytes=readFileSync(f.path),r=new FtsRetriever(openStore(f.directory)),before=originals(r.db);
 assert.equal(r.validity.prepared,false);assert.deepEqual(r.search('bicycle').map(row=>row.id),[7]);assert.throws(()=>r.change({id:7,action:'invalidate',expectedRevision:0,reason:'owned',mutationId:randomUUID()},{source:'paired-client'}),e=>e.code==='MEMORY_STATE_UNAVAILABLE');r.close();
 assert.deepEqual(readFileSync(f.path),bytes,'Opening legacy state must not silently migrate it');
 const result=await maintenance.prepareMemoryValidity({databasePath:f.path,backupPath:f.backup});assert.equal(result.prepared,true);assert.match(result.backupSha256,/^[a-f0-9]{64}$/);
 const backup=new DatabaseSync(f.backup,{readOnly:true});try{assert.deepEqual(originals(backup),before);assert.equal(backup.prepare("SELECT name FROM sqlite_master WHERE name='memory_validity_schema'").get(),undefined);}finally{backup.close();}
 const reopened=new FtsRetriever(openStore(f.directory));try{assert.equal(reopened.validity.prepared,true);assert.deepEqual(originals(reopened.db),before);assert.equal(reopened.describe(7).revision,0);}finally{reopened.close();}
 const duplicate=join(f.directory,'duplicate.db');await assert.rejects(maintenance.prepareMemoryValidity({databasePath:f.path,backupPath:duplicate}));assert.equal(existsSync(duplicate),false);
});

test('failed preparation rolls back all new schema and retains the original backup',async t=>{
 const f=fixture(t);legacy(f.path);let db=new DatabaseSync(f.path);db.exec('CREATE INDEX memory_audit_record ON memories(kind)');const before=originals(db);db.close();
 await assert.rejects(maintenance.prepareMemoryValidity({databasePath:f.path,backupPath:f.backup}));assert.ok(existsSync(f.backup));
 db=new DatabaseSync(f.path);try{assert.deepEqual(originals(db),before);assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE name='memory_validity_schema'").get(),undefined);assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE name='memory_state'").get(),undefined);assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE name='memory_audit'").get(),undefined);assert.ok(db.prepare("SELECT name FROM sqlite_master WHERE name='memory_audit_record' AND type='index'").get());}finally{db.close();}
});
