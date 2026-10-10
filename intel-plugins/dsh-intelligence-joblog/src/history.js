import {DatabaseSync} from 'node:sqlite';
import {existsSync,lstatSync,openSync,closeSync,writeFileSync,renameSync,unlinkSync,fsyncSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {withStoreWriter} from '../../shared/persistence.js';

export function storageError(code,operation,error){
  const errno=typeof error?.code==='string'&&/^[A-Z0-9_]{1,80}$/.test(error.code)?error.code:undefined;
  return Object.assign(new Error(code),{code,operation,...(errno?{errno}:{})});
}

export function atomicWrite(file,text){
  if(Buffer.byteLength(text)>512*1024)throw storageError('JOBLOG_TOO_LARGE','latest');
  const temporary=join(dirname(file),'.joblog-'+randomUUID()+'.tmp');
  let fd;
  try{
    fd=openSync(temporary,'wx',0o600);writeFileSync(fd,text);fsyncSync(fd);closeSync(fd);fd=undefined;
    renameSync(temporary,file);
    const directory=openSync(dirname(file),'r');try{fsyncSync(directory);}finally{closeSync(directory);}
  }finally{
    if(fd!==undefined)closeSync(fd);
    if(existsSync(temporary))unlinkSync(temporary);
  }
}

function openHistory(file,readOnly){
  if(existsSync(file)&&(!lstatSync(file).isFile()||lstatSync(file).size>16*1024*1024))
    throw storageError('JOBLOG_INVALID_DATA','history');
  if(!readOnly){try{closeSync(openSync(file,'wx',0o600));}catch(error){if(error.code!=='EEXIST')throw error;}}
  const db=new DatabaseSync(file,{readOnly});
  try{
    db.exec('PRAGMA busy_timeout=5000');
    if(!readOnly)db.exec('BEGIN IMMEDIATE');
    const version=db.prepare('PRAGMA user_version').get().user_version;
    if(version!==1){
      if(readOnly||version!==0||db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().length)
        throw storageError('JOBLOG_INVALID_DATA','history');
      db.exec(`
        CREATE TABLE runs(seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,runId TEXT NOT NULL,
          row TEXT NOT NULL,UNIQUE(scope,runId));
        CREATE TABLE latest(scope TEXT NOT NULL,id TEXT NOT NULL,row TEXT NOT NULL,PRIMARY KEY(scope,id));
        CREATE TABLE legacy(id TEXT PRIMARY KEY,row TEXT NOT NULL);
        PRAGMA user_version=1;`);
    }
    if(!readOnly)db.exec('COMMIT');
    return db;
  }catch(error){db.close();throw error;}
}

/** Transactional completion store. Legacy snapshots retain their original fields and source. */
export function persistRun(file,row,limit,readProjection,writeProjection){
  let db,transaction=false,committed=false;
  try{
    return withStoreWriter(dirname(file),()=>{
    db=openHistory(file,false);db.exec('BEGIN IMMEDIATE');transaction=true;
    const previous=db.prepare('SELECT row FROM runs WHERE scope=? AND runId=?').get(row.scope,row.runId);
    if(previous){
      const old=JSON.parse(previous.row);
      if(old.id!==row.id||old.status!==row.status||old.detail!==row.detail)
        throw storageError('JOBLOG_IDENTITY_COLLISION','completion');
      // A retry also repairs a failed legacy projection without counting another run.
      const projection=readProjection();
      for(const current of db.prepare('SELECT row FROM latest WHERE scope=?').all(row.scope)){
        const value=JSON.parse(current.row);projection[value.id]=value;
      }
      db.exec('COMMIT');transaction=false;committed=true;
      writeProjection(projection);return old;
    }
    const projection=readProjection();
    for(const [id,old]of Object.entries(projection)){
      if(!old.runId)db.prepare('INSERT OR IGNORE INTO legacy(id,row) VALUES (?,?)').run(JSON.stringify([old.scope??'unclassified',id]),JSON.stringify({...old,id,scope:old.scope??'unclassified',detail:old.detail??'',legacy:true}));
    }
    db.prepare('INSERT INTO runs(scope,runId,row) VALUES (?,?,?)').run(row.scope,row.runId,JSON.stringify(row));
    db.prepare('INSERT OR REPLACE INTO latest(scope,id,row) VALUES (?,?,?)').run(row.scope,row.id,JSON.stringify(row));
    db.prepare('DELETE FROM runs WHERE scope=? AND seq NOT IN (SELECT seq FROM runs WHERE scope=? ORDER BY seq DESC LIMIT ?)').run(row.scope,row.scope,limit);
    projection[row.id]=row;
    db.exec('COMMIT');transaction=false;committed=true;
    writeProjection(projection);return row;
    });
  }catch(error){
    if(transaction)db.exec('ROLLBACK');
    if(error.code?.startsWith('JOBLOG_'))throw Object.assign(error,{committed});
    throw Object.assign(storageError('JOBLOG_WRITE_FAILED','completion',error),{committed});
  }finally{db?.close();}
}

/** Read committed records; malformed databases fail instead of falling back to latest. */
export function storedRows(file,table){
  if(!existsSync(file))return [];
  let db;
  try{
    db=openHistory(file,true);
    const query=table==='runs'?'SELECT row FROM runs ORDER BY seq DESC':table==='latest'?'SELECT row FROM latest':'SELECT row FROM legacy';
    return db.prepare(query).all().map(({row})=>{
      const value=JSON.parse(row);
      if(!value||typeof value!=='object'||Array.isArray(value)||typeof value.id!=='string'||
        typeof value.last!=='string'||typeof value.status!=='string'||typeof value.detail!=='string'||
        !['production','test','unclassified'].includes(value.scope)||
        (table!=='legacy'&&(typeof value.runId!=='string'||!value.runId||value.runId.length>256||
          !['ok','fail'].includes(value.status)||typeof value.finishedAt!=='string'||!Number.isFinite(Date.parse(value.finishedAt))||
          value.detail.length>2048||typeof value.summary!=='string'||value.summary.length>200||typeof value.detailTruncated!=='boolean')))
        throw storageError('JOBLOG_INVALID_DATA','history');
      return value;
    });
  }catch(error){
    if(error.code?.startsWith('JOBLOG_'))throw error;
    throw storageError('JOBLOG_READ_FAILED','history',error);
  }finally{db?.close();}
}
