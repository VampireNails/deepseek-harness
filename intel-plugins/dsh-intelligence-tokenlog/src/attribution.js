import {DatabaseSync} from 'node:sqlite';
import {lstatSync} from 'node:fs';
import {join,basename,dirname} from 'node:path';

const taskId=value=>typeof value==='string'&&/^[a-z][a-z0-9_-]{0,79}$/.test(value);
const runId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
function read(file,limit,query){
  if(!file)return;
  let stat;try{stat=lstatSync(file);}catch(error){if(error.code==='ENOENT')return;throw Error('TOKENLOG_METADATA_UNREADABLE');}
  if(!stat.isFile()||stat.size>limit)throw Error('TOKENLOG_METADATA_INVALID');
  let db;
  try{
    db=new DatabaseSync(file,{readOnly:true});db.exec('PRAGMA busy_timeout=3000; BEGIN');
    if(db.prepare('PRAGMA user_version').get().user_version!==1)throw Error('version');
    const rows=query(db);db.exec('COMMIT');return rows;
  }catch{throw Error('TOKENLOG_METADATA_INVALID');}finally{db?.close();}
}

/** Snapshot existing identity and completion stores read-only; completion time never selects usage. */
export function readAttribution(sessionsRoot,{automationIndex,jobHistory}={}){
  const indexFile=automationIndex??(basename(sessionsRoot)==='--root--'&&basename(dirname(sessionsRoot))==='sessions'
    ?join(dirname(dirname(sessionsRoot)),'intel-joblog','automation-sessions.sqlite'):undefined);
  const identities=read(indexFile,32*1024*1024,db=>db.prepare('SELECT session_id,task_id,run_id,run_date FROM automation_sessions').all());
  const sessions=new Map();
  for(const row of identities??[]){
    const date=row.run_date;
    if(typeof row.session_id!=='string'||!row.session_id||row.session_id.length>1024||!taskId(row.task_id)||!runId(row.run_id)||
      typeof date!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(date)||!Number.isFinite(Date.parse(date+'T00:00:00Z'))||
      new Date(date+'T00:00:00Z').toISOString().slice(0,10)!==date||sessions.has(row.session_id))throw Error('TOKENLOG_METADATA_INVALID');
    sessions.set(row.session_id,row);
  }
  const history=read(jobHistory??(indexFile?join(dirname(indexFile),'job_history.sqlite'):undefined),16*1024*1024,
    db=>[...db.prepare('SELECT scope,runId,row FROM runs').all(),...db.prepare('SELECT scope,id,row FROM latest').all()]);
  const runs=new Map();
  for(const entry of history??[]){
    let row;try{row=JSON.parse(entry.row);}catch{throw Error('TOKENLOG_METADATA_INVALID');}
    if(!row||!taskId(row.id)||!runId(row.runId)||!['production','test'].includes(row.scope)||row.scope!==entry.scope||
      (entry.runId!==undefined&&entry.runId!==row.runId)||(entry.id!==undefined&&entry.id!==row.id))throw Error('TOKENLOG_METADATA_INVALID');
    if(!runs.has(row.runId))runs.set(row.runId,new Map());
    runs.get(row.runId).set(JSON.stringify([row.id,row.scope]),{id:row.id,scope:row.scope});
  }
  return {sessions,runs,indexAvailable:identities!==undefined,scopeAvailable:history!==undefined};
}

/** Resolve exact subagent ancestry; ordinary forks do not inherit an automation identity. */
export function nativeIdentity(header,headers,attribution,seen=new Set()){
  if(!header)return;
  if(seen.has(header.id))return {conflict:true};
  seen.add(header.id);
  const direct=attribution.sessions.get(header.id);
  let parent;
  if(header.origin==='subagent')parent=nativeIdentity(headers.get(header.parentSession),headers,attribution,seen);
  if(parent?.conflict||direct&&parent&&(direct.task_id!==parent.task_id||direct.run_id!==parent.run_id))return {conflict:true};
  return direct??parent;
}

/** Match immutable task/run to committed scoped completion; absent/pruned/pending runs remain unknown. */
export function scopeIdentity(identity,attribution){
  if(identity?.conflict)return {scope:'unclassified',scopeStatus:'conflict',scopeSource:'automation-index-v1'};
  const matches=identity&&attribution.runs.get(identity.run_id);
  if(!matches?.size)return {scope:'unclassified',scopeStatus:'unknown',scopeSource:'none'};
  const rows=[...matches.values()];
  if(rows.length!==1||rows[0].id!==identity.task_id)return {scope:'unclassified',scopeStatus:'conflict',scopeSource:'job-history-v1',taskConflict:rows.some(row=>row.id!==identity.task_id)};
  return {scope:rows[0].scope,scopeStatus:'known',scopeSource:'job-history-v1'};
}
