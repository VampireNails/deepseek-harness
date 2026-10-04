import {DatabaseSync} from 'node:sqlite';
import {existsSync,mkdirSync,chmodSync} from 'node:fs';
import {join} from 'node:path';

const SCHEMA_VERSION=1;
const INDEX_FILENAME='automation-sessions.sqlite';

/** Read only runner-supplied identity; ordinary processes have no identity. */
export function runnerIdentity(env=process.env){
  const taskId=env.INTEL_TASK_ID,runId=env.INTEL_RUN_ID,runDate=env.INTEL_RUN_DATE;
  if(taskId===undefined&&runId===undefined&&runDate===undefined)return null;
  if(typeof taskId!=='string'||!/^[a-z][a-z0-9_-]{0,79}$/.test(taskId)||
    typeof runId!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(runId)||
    typeof runDate!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(runDate))throw Error('invalid or incomplete runner identity');
  const date=new Date(runDate+'T00:00:00.000Z');
  if(!Number.isFinite(date.getTime())||date.toISOString().slice(0,10)!==runDate)throw Error('invalid runner date');
  return Object.freeze({taskId,runId,runDate});
}

/** Owns transactional, immutable root-session attribution in the shared joblog directory. */
export class AutomationSessionStore{
  /** @param directory - DSH_HOME/intel-joblog; busyTimeoutMs bounds competing SQLite writers. */
  constructor(directory,{busyTimeoutMs=3000}={}){
    if(!Number.isInteger(busyTimeoutMs)||busyTimeoutMs<1||busyTimeoutMs>30000)throw Error('invalid automation index busy timeout');
    mkdirSync(directory,{recursive:true,mode:0o700});
    const file=join(directory,INDEX_FILENAME);
    this.db=new DatabaseSync(file);
    try{
      chmodSync(file,0o600);
      this.db.exec('PRAGMA busy_timeout='+busyTimeoutMs);
      this.db.exec('BEGIN IMMEDIATE');
      const version=this.db.prepare('PRAGMA user_version').get().user_version;
      if(version!==0&&version!==SCHEMA_VERSION)throw Error('unsupported automation index schema');
      this.db.exec(`CREATE TABLE IF NOT EXISTS automation_sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        run_date TEXT NOT NULL
      ); PRAGMA user_version=${SCHEMA_VERSION}; COMMIT;`);
    }catch(error){this.db.close();throw error;}
  }

  /** Record one created root; a repeated identity is idempotent and another identity cannot replace it. */
  record(sessionId,identity){
    if(typeof sessionId!=='string'||!sessionId||sessionId.length>1024)throw Error('invalid automation session id');
    const normalized=runnerIdentity({INTEL_TASK_ID:identity?.taskId,INTEL_RUN_ID:identity?.runId,INTEL_RUN_DATE:identity?.runDate});
    if(!normalized)throw Error('invalid automation session identity');
    this.db.exec('BEGIN IMMEDIATE');
    try{
      const previous=this.db.prepare('SELECT task_id,run_id,run_date FROM automation_sessions WHERE session_id=?').get(sessionId);
      if(previous){
        if(previous.task_id!==normalized.taskId||previous.run_id!==normalized.runId||previous.run_date!==normalized.runDate)
          throw Error('automation session identity conflict');
      }else this.db.prepare('INSERT INTO automation_sessions(session_id,task_id,run_id,run_date) VALUES (?,?,?,?)')
        .run(sessionId,normalized.taskId,normalized.runId,normalized.runDate);
      this.db.exec('COMMIT');
    }catch(error){this.db.exec('ROLLBACK');throw error;}
  }

  /** Release the writer after the event listener has been removed. */
  close(){this.db.close();}
}

/** Open DSH_HOME/intel-joblog read-only; absence returns no attribution and creates no files. */
export function readAutomationSessions(directory){
  const file=join(directory,INDEX_FILENAME);
  if(!existsSync(file))return [];
  const db=new DatabaseSync(file,{readOnly:true});
  try{
    if(db.prepare('PRAGMA user_version').get().user_version!==SCHEMA_VERSION)throw Error('unsupported automation index schema');
    return db.prepare('SELECT session_id AS sessionId, task_id AS taskId, run_id AS runId, run_date AS runDate FROM automation_sessions ORDER BY session_id')
      .all().map(row=>({...row}));
  }finally{db.close();}
}
