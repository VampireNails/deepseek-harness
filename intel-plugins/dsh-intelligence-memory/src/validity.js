import { createHash, randomUUID } from 'node:crypto';

const failure = code => Object.assign(new Error(code), { code });
const safeId = value => Number.isSafeInteger(value) && value > 0;

/** Existing stores are prepared explicitly; opening them never silently adds state. */
export function hasValiditySchema(db) {
  if (!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_validity_schema'").get()) {
    if(db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('memory_state','memory_audit')").get())throw failure('MEMORY_SCHEMA_INVALID');
    return false;
  }
  const rows=db.prepare('SELECT version FROM memory_validity_schema').all();
  if(rows.length!==1||rows[0].version!==1)throw failure('MEMORY_SCHEMA_UNSUPPORTED');
  for(const name of ['memory_state','memory_audit'])
    if(!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name))throw failure('MEMORY_SCHEMA_INVALID');
  return true;
}

/** The caller owns the schema transaction and any required pre-migration backup. */
export function createValiditySchema(db) {
  db.exec(`
    CREATE TABLE memory_validity_schema(version INTEGER NOT NULL);
    INSERT INTO memory_validity_schema VALUES(1);
    CREATE TABLE memory_state(
      memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
      active INTEGER NOT NULL CHECK(active IN (0,1)),
      revision INTEGER NOT NULL CHECK(revision>0),
      changed_at INTEGER NOT NULL,
      reason TEXT NOT NULL,
      source TEXT NOT NULL,
      session_id TEXT
    );
    CREATE TABLE memory_audit(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      memory_id INTEGER NOT NULL REFERENCES memories(id),
      action TEXT NOT NULL CHECK(action IN ('invalidate','restore')),
      revision INTEGER NOT NULL CHECK(revision>0),
      changed_at INTEGER NOT NULL,
      reason TEXT NOT NULL,
      source TEXT NOT NULL,
      session_id TEXT,
      mutation_id TEXT NOT NULL UNIQUE,
      request_hash TEXT NOT NULL,
      result TEXT NOT NULL
    );
    CREATE INDEX memory_audit_record ON memory_audit(memory_id,id);
  `);
}

function validateCommand(args) {
  if(!args||!safeId(args.id)||!['invalidate','restore'].includes(args.action)||
      !Number.isSafeInteger(args.expectedRevision)||args.expectedRevision<0||
      typeof args.reason!=='string'||!args.reason.trim()||args.reason.length>2048||
      /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(args.reason)||
      typeof args.mutationId!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(args.mutationId))
    throw failure('MEMORY_INVALID_ARGUMENT');
  return {id:args.id,action:args.action,expectedRevision:args.expectedRevision,reason:args.reason.trim(),mutationId:args.mutationId};
}

function validateActor(actor) {
  if(!actor||!['paired-client','approved-tool'].includes(actor.source)||
      (actor.sessionId!==undefined&&(typeof actor.sessionId!=='string'||!actor.sessionId||actor.sessionId.length>1024)))
    throw failure('MEMORY_INVALID_ACTOR');
  if(actor.signal?.aborted)throw failure('MEMORY_CANCELLED');
  return {source:actor.source,sessionId:actor.sessionId??null};
}

/** Immutable original rows plus separately audited, revision-controlled validity. */
export class MemoryValidity {
  constructor(db,indexText) {
    this.db=db;this.indexText=indexText;this.prepared=hasValiditySchema(db);
  }

  describe(id) {
    if(!safeId(id))throw failure('MEMORY_INVALID_ARGUMENT');
    const row=this.db.prepare('SELECT id,text,kind,created_at FROM memories WHERE id=?').get(id);
    if(!row)return null;
    const state=this.prepared?this.db.prepare('SELECT active,revision,changed_at,reason,source FROM memory_state WHERE memory_id=?').get(id):null;
    return {id:row.id,text:row.text,kind:row.kind,createdAt:row.created_at,active:state?state.active===1:true,
      revision:state?.revision??0,changedAt:state?.changed_at??null,reason:state?.reason??null,source:state?.source??null};
  }

  history(id) {
    if(!safeId(id))throw failure('MEMORY_INVALID_ARGUMENT');
    if(!this.prepared)return [];
    return this.db.prepare('SELECT action,revision,changed_at AS changedAt,reason,source,mutation_id AS mutationId FROM memory_audit WHERE memory_id=? ORDER BY id DESC LIMIT 50').all(id).map(row=>({...row}));
  }

  list({mode='active',query='',limit=30,beforeId}={}) {
    if(!['active','invalidated','all'].includes(mode)||typeof query!=='string'||query.length>512||
        !Number.isSafeInteger(limit)||limit<1||limit>50||(beforeId!==undefined&&!safeId(beforeId)))throw failure('MEMORY_INVALID_ARGUMENT');
    if(!this.prepared&&mode!=='active')throw failure('MEMORY_STATE_UNAVAILABLE');
    const state=this.prepared?'LEFT JOIN memory_state s ON s.memory_id=m.id':'',active=this.prepared?'COALESCE(s.active,1)':'1';
    const conditions=[],values=[];
    if(mode!=='all'){conditions.push(active+'=?');values.push(mode==='active'?1:0);}
    if(query.trim()){conditions.push('instr(lower(m.text),lower(?))>0');values.push(query.trim());}
    if(beforeId!==undefined){conditions.push('m.id<?');values.push(beforeId);}
    const rows=this.db.prepare('SELECT m.id,m.text,m.kind,m.created_at,'+active+' AS active FROM memories m '+state+(conditions.length?' WHERE '+conditions.join(' AND '):'')+' ORDER BY m.id DESC LIMIT ?').all(...values,limit+1);
    const items=rows.slice(0,limit).map(row=>({id:row.id,preview:row.text.slice(0,240),previewComplete:row.text.length<=240,kind:row.kind,createdAt:row.created_at,active:row.active===1}));
    return {items,hasMore:rows.length>limit,nextBeforeId:rows.length>limit?items.at(-1).id:null};
  }

  change(input,context) {
    const args=validateCommand(input),actor=validateActor(context);
    if(!this.prepared)throw failure('MEMORY_STATE_UNAVAILABLE');
    const digest=createHash('sha256').update(JSON.stringify({...args,...actor})).digest('hex');
    const savepoint='memory_validity_'+randomUUID().replaceAll('-','');
    this.db.exec('SAVEPOINT '+savepoint);
    try{
      const previous=this.db.prepare('SELECT request_hash,result FROM memory_audit WHERE mutation_id=?').get(args.mutationId);
      if(previous){
        if(previous.request_hash!==digest)throw failure('MEMORY_MUTATION_ID_REUSED');
        const result=JSON.parse(previous.result);this.db.exec('RELEASE '+savepoint);return result;
      }
      const current=this.describe(args.id);if(!current)throw failure('MEMORY_NOT_FOUND');
      if(current.revision!==args.expectedRevision)throw failure('MEMORY_REVISION_CONFLICT');
      const active=args.action==='restore';if(current.active===active)throw failure('MEMORY_STATE_CONFLICT');
      if(context.signal?.aborted)throw failure('MEMORY_CANCELLED');
      const revision=current.revision+1;if(!Number.isSafeInteger(revision))throw failure('MEMORY_REVISION_CONFLICT');
      const changedAt=Date.now();
      this.db.prepare('INSERT INTO memory_state(memory_id,active,revision,changed_at,reason,source,session_id) VALUES(?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET active=excluded.active,revision=excluded.revision,changed_at=excluded.changed_at,reason=excluded.reason,source=excluded.source,session_id=excluded.session_id')
        .run(args.id,active?1:0,revision,changedAt,args.reason,actor.source,actor.sessionId);
      this.db.prepare('DELETE FROM memories_fts WHERE rowid=?').run(args.id);
      if(active)this.db.prepare('INSERT INTO memories_fts(rowid,text) VALUES(?,?)').run(args.id,this.indexText(current.text));
      const result={record:{id:args.id,active,revision},changed:true};
      this.db.prepare('INSERT INTO memory_audit(memory_id,action,revision,changed_at,reason,source,session_id,mutation_id,request_hash,result) VALUES(?,?,?,?,?,?,?,?,?,?)')
        .run(args.id,args.action,revision,changedAt,args.reason,actor.source,actor.sessionId,args.mutationId,digest,JSON.stringify(result));
      this.db.exec('RELEASE '+savepoint);return result;
    }catch(error){
      try{this.db.exec('ROLLBACK TO '+savepoint+'; RELEASE '+savepoint);}catch{throw failure('MEMORY_ROLLBACK_FAILED');}
      if(typeof error.code==='string'&&error.code.startsWith('MEMORY_'))throw error;
      throw failure('MEMORY_WRITE_FAILED');
    }
  }
}
