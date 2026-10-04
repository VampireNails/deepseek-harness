import {createHash} from 'node:crypto';
import {chmodSync,closeSync,existsSync,openSync,readFileSync} from 'node:fs';
import {isAbsolute,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {DatabaseSync,backup} from 'node:sqlite';
import {runnerIdentity} from '../../intel-plugins/dsh-intelligence-evolution/src/session-origin.js';

const RECEIPT_VERSION=1;
const PREFIX='legacy-evidence:';
const HASH=/^[a-f0-9]{64}$/;
const hash=value=>createHash('sha256').update(value).digest('hex');
const sessionHash=id=>hash('round13-legacy-origin:'+id);
const same=(row,candidate)=>row.task_id===candidate.taskId&&row.run_id===candidate.runId&&row.run_date===candidate.runDate;
const dateOf=time=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(time));

function identity(candidate){
  if(typeof candidate.sessionId!=='string'||!candidate.sessionId||candidate.sessionId.length>1024)
    throw Error('invalid legacy session identity');
  runnerIdentity({INTEL_TASK_ID:candidate.taskId,INTEL_RUN_ID:candidate.runId,INTEL_RUN_DATE:candidate.runDate});
  if(!new RegExp('^'+PREFIX+'[a-f0-9]{64}$').test(candidate.runId))throw Error('legacy evidence identity required');
}

function intervalIdentity(interval){
  if(typeof interval.source!=='string'||!interval.source||!HASH.test(interval.sourceHash)||
    !Number.isSafeInteger(interval.startLine)||interval.startLine<1||!Number.isSafeInteger(interval.finishLine)||interval.finishLine<=interval.startLine||
    !Number.isSafeInteger(interval.start)||!Number.isSafeInteger(interval.finish)||interval.start>interval.finish)
    throw Error('invalid legacy audit interval');
  const runId=PREFIX+hash(JSON.stringify([interval.source,interval.startLine,interval.finishLine,interval.taskId,interval.start,interval.finish]));
  const runDate=dateOf(interval.start);
  runnerIdentity({INTEL_TASK_ID:interval.taskId,INTEL_RUN_ID:runId,INTEL_RUN_DATE:runDate});
  return {taskId:interval.taskId,runId,runDate};
}

/**
 * Extract joint-evidence candidates in memory. Header and firstUser come from the v4 log,
 * firstUser is the first complete source.kind=user event, templates include documented stdin wrapping,
 * and intervals have already been paired from exact start/finish audit records.
 * Counts include every competing root before excluding indexed or ambiguous observations.
 * @param input - Private observations, exact templates, paired intervals, and current indexed IDs.
 * @returns Private candidate IDs plus a public receipt containing hashes only.
 */
export function collectLegacyCandidates({sessions,templates,intervals,indexedSessionIds=[]}){
  if(!Array.isArray(sessions)||!Array.isArray(templates)||!Array.isArray(intervals)||!Array.isArray(indexedSessionIds))
    throw Error('invalid legacy observations');
  const catalog=new Map();
  for(const template of templates){
    if(typeof template.prompt!=='string'||!template.prompt)throw Error('invalid complete legacy template');
    runnerIdentity({INTEL_TASK_ID:template.taskId,INTEL_RUN_ID:'template-validation',INTEL_RUN_DATE:'2000-01-01'});
    if(!catalog.has(template.prompt))catalog.set(template.prompt,new Set());
    catalog.get(template.prompt).add(template.taskId);
  }
  const metadata=intervals.map(intervalIdentity);
  const indexed=new Set(indexedSessionIds),multiplicity=new Map(),competing=intervals.map(()=>new Set());
  const observations=[],counts={observations:sessions.length,roots:0,subagents:0,unknown:0,alreadyIndexed:0,exactTemplates:0,
    noInterval:0,multipleIntervals:0,multipleRoots:0,duplicateIds:0};
  for(const session of sessions){
    const header=session.header;
    if(session.generation!==4||typeof header?.id!=='string'||!header.id)throw Error('invalid v4 session observation');
    multiplicity.set(header.id,(multiplicity.get(header.id)||0)+1);
    if(header.origin==='subagent'){counts.subagents++;continue;}
    counts.roots++;
    const user=session.firstUser,parts=user?.data?.content;
    const taskIds=user?.type==='user/message'&&user.data?.source?.kind==='user'&&Array.isArray(parts)&&parts.length===1&&
      parts[0]?.type==='text'&&typeof parts[0].text==='string'?catalog.get(parts[0].text):undefined;
    if(!taskIds){observations.push({id:header.id,windows:[]});continue;}
    counts.exactTemplates++;
    const windows=[];
    if(Number.isSafeInteger(header.createdAt)&&Number.isSafeInteger(user.time)&&user.time>=header.createdAt){
      for(let i=0;i<intervals.length;i++){
        const interval=intervals[i];
        if(taskIds.has(interval.taskId)&&header.createdAt>=interval.start&&header.createdAt<=interval.finish&&
          user.time>=interval.start&&user.time<=interval.finish){windows.push(i);competing[i].add(header.id);}
      }
    }
    observations.push({id:header.id,windows,promptHash:hash(parts[0].text)});
  }
  const candidates=[];
  for(const observation of observations){
    if(indexed.has(observation.id)){counts.alreadyIndexed++;continue;}
    if(multiplicity.get(observation.id)!==1){counts.duplicateIds++;continue;}
    if(observation.windows.length===0){counts.noInterval++;continue;}
    if(observation.windows.length!==1){counts.multipleIntervals++;continue;}
    const slot=observation.windows[0];
    if(competing[slot].size!==1){counts.multipleRoots++;continue;}
    candidates.push({sessionId:observation.id,...metadata[slot],evidence:{promptHash:observation.promptHash,
      source:intervals[slot].source,sourceHash:intervals[slot].sourceHash,startLine:intervals[slot].startLine,finishLine:intervals[slot].finishLine}});
  }
  counts.unknown=counts.roots-counts.alreadyIndexed-candidates.length;
  return {candidates,receipt:{version:RECEIPT_VERSION,kind:'legacy-evidence-dry-run',counts,
    candidates:candidates.map(candidate=>({sessionHash:sessionHash(candidate.sessionId),taskId:candidate.taskId,
      runId:candidate.runId,runDate:candidate.runDate,evidence:{promptHash:candidate.evidence.promptHash,
        sourceHash:candidate.evidence.sourceHash,startLine:candidate.evidence.startLine,finishLine:candidate.evidence.finishLine}}))}};
}

function checkSchema(db){
  if(db.prepare('PRAGMA user_version').get().user_version!==1)throw Error('unsupported automation index schema');
  db.prepare('SELECT session_id,task_id,run_id,run_date FROM automation_sessions LIMIT 0').all();
}

/** Read the index without creating files; opaque IDs must remain in the caller's private memory. */
export function readLegacyIndexIds(indexPath){
  if(!existsSync(indexPath))return [];
  const db=new DatabaseSync(indexPath,{readOnly:true});
  try{checkSchema(db);return db.prepare('SELECT session_id FROM automation_sessions').all().map(row=>row.session_id);}
  finally{db.close();}
}

/**
 * Backup an existing index, then insert one verified batch in an immediate transaction.
 * Conflicts reject the entire batch; identical rows are reused and are absent from rollback entries.
 * Backup restoration is never used for rollback because it could delete later native records.
 * @param options - Explicit index/backup paths and private candidates from collectLegacyCandidates.
 * @returns An anonymous receipt listing only rows inserted by this invocation.
 */
export async function applyLegacyCandidates({indexPath,backupPath,candidates}){
  if(!isAbsolute(indexPath)||!isAbsolute(backupPath)||resolve(indexPath)===resolve(backupPath)||existsSync(backupPath))
    throw Error('distinct absolute index and unused backup paths required');
  if(!Array.isArray(candidates))throw Error('invalid candidate batch');
  const seen=new Set();
  for(const candidate of candidates){identity(candidate);if(seen.has(candidate.sessionId))throw Error('duplicate candidate session');seen.add(candidate.sessionId);}
  if(candidates.length===0)return {version:RECEIPT_VERSION,kind:'legacy-evidence-apply',
    backup:{status:'no-change',pathHash:null,sha256:null},candidates:0,inserted:0,reused:0,rows:[]};
  const existed=existsSync(indexPath);
  let backupReceipt={status:'index-absent',pathHash:null,sha256:null};
  if(existed){
    const source=new DatabaseSync(indexPath,{readOnly:true});
    try{
      checkSchema(source);
      closeSync(openSync(backupPath,'wx',0o600));
      await backup(source,backupPath);
    }finally{source.close();}
    chmodSync(backupPath,0o600);
    backupReceipt={status:'created',pathHash:hash(backupPath),sha256:hash(readFileSync(backupPath))};
  }
  const db=new DatabaseSync(indexPath),inserted=[];
  try{
    chmodSync(indexPath,0o600);
    db.exec('PRAGMA busy_timeout=3000; BEGIN IMMEDIATE');
    try{
      if(!existed)db.exec('CREATE TABLE automation_sessions(session_id TEXT PRIMARY KEY NOT NULL,task_id TEXT NOT NULL,run_id TEXT NOT NULL,run_date TEXT NOT NULL); PRAGMA user_version=1;');
      checkSchema(db);
      const get=db.prepare('SELECT task_id,run_id,run_date FROM automation_sessions WHERE session_id=?');
      const insert=db.prepare('INSERT INTO automation_sessions(session_id,task_id,run_id,run_date) VALUES (?,?,?,?)');
      for(const candidate of candidates){
        const previous=get.get(candidate.sessionId);
        if(previous){if(!same(previous,candidate))throw Error('automation session identity conflict');continue;}
        insert.run(candidate.sessionId,candidate.taskId,candidate.runId,candidate.runDate);
        inserted.push({sessionHash:sessionHash(candidate.sessionId),taskId:candidate.taskId,runId:candidate.runId,runDate:candidate.runDate});
      }
      db.exec('COMMIT');
    }catch(error){db.exec('ROLLBACK');throw error;}
  }finally{db.close();}
  return {version:RECEIPT_VERSION,kind:'legacy-evidence-apply',backup:backupReceipt,
    candidates:candidates.length,inserted:inserted.length,reused:candidates.length-inserted.length,rows:inserted};
}

/**
 * Remove only this receipt's inserted rows whose complete identity still matches.
 * Changed identities and every unlisted/native row remain intact; no SQLite backup is restored.
 * @param options - Explicit index path and anonymous apply receipt.
 * @returns Anonymous deleted/changed/missing counts.
 */
export function rollbackLegacyCandidates({indexPath,receipt}){
  if(!isAbsolute(indexPath)||receipt?.version!==RECEIPT_VERSION||receipt.kind!=='legacy-evidence-apply'||!Array.isArray(receipt.rows))
    throw Error('invalid migration rollback receipt');
  const expected=new Map();
  for(const row of receipt.rows){
    identity({...row,sessionId:'receipt-validation'});
    if(!HASH.test(row.sessionHash)||expected.has(row.sessionHash))throw Error('invalid migration rollback identity');
    expected.set(row.sessionHash,row);
  }
  if(!existsSync(indexPath))return {version:RECEIPT_VERSION,kind:'legacy-evidence-rollback',deleted:0,changed:0,missing:expected.size};
  const db=new DatabaseSync(indexPath);
  let deleted=0,changed=0,found=0;
  try{
    db.exec('PRAGMA busy_timeout=3000; BEGIN IMMEDIATE');
    try{
      checkSchema(db);
      const remove=db.prepare('DELETE FROM automation_sessions WHERE session_id=? AND task_id=? AND run_id=? AND run_date=?');
      for(const row of db.prepare('SELECT session_id,task_id,run_id,run_date FROM automation_sessions').all()){
        const entry=expected.get(sessionHash(row.session_id));
        if(!entry)continue;
        found++;
        if(!same(row,entry)){changed++;continue;}
        deleted+=Number(remove.run(row.session_id,entry.taskId,entry.runId,entry.runDate).changes);
      }
      db.exec('COMMIT');
    }catch(error){db.exec('ROLLBACK');throw error;}
  }finally{db.close();}
  return {version:RECEIPT_VERSION,kind:'legacy-evidence-rollback',deleted,changed,missing:expected.size-found};
}

/**
 * Explicit offline maintenance CLI. Input observations arrive on stdin and are never printed.
 * dry-run: --index /absolute/index.sqlite; apply additionally requires --backup /absolute/new.sqlite;
 * rollback reads an anonymous apply receipt on stdin. There are no production path defaults.
 * @param argv - Mode and named paths.
 * @returns Anonymous JSON receipt, or throws a content-free diagnostic.
 */
export async function legacyOriginCli(argv){
  const [mode,...args]=argv;
  if(!['dry-run','apply','rollback'].includes(mode)||args.length%2)throw Error('invalid legacy migration arguments');
  const options={};
  for(let i=0;i<args.length;i+=2){
    if(!['--index','--backup'].includes(args[i])||options[args[i]])throw Error('invalid legacy migration arguments');
    options[args[i]]=args[i+1];
  }
  if(!options['--index']||!isAbsolute(options['--index']))throw Error('explicit absolute index path required');
  let input;try{input=JSON.parse(readFileSync(0,'utf8'));}catch{throw Error('invalid private migration input');}
  if(mode==='rollback')return rollbackLegacyCandidates({indexPath:options['--index'],receipt:input});
  const collected=collectLegacyCandidates({...input,indexedSessionIds:mode==='dry-run'?readLegacyIndexIds(options['--index']):[]});
  if(mode==='dry-run')return collected.receipt;
  if(!options['--backup'])throw Error('explicit backup path required');
  return applyLegacyCandidates({indexPath:options['--index'],backupPath:options['--backup'],candidates:collected.candidates});
}

if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
  try{console.log(JSON.stringify(await legacyOriginCli(process.argv.slice(2)),null,2));}
  catch{console.error('Legacy migration failed; inspect private inputs and index state.');process.exitCode=1;}
}
