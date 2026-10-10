// Private cron/goals persistence: JSON owns records; the existing writer DB owns exclusion.
import {DatabaseSync} from 'node:sqlite';
import {readFileSync,lstatSync,statSync,openSync,writeFileSync,fsyncSync,closeSync,renameSync,unlinkSync,realpathSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {withStoreWriter} from './persistence.js';

export function persistenceError(prefix,kind,error,committed=false){
 const code=`${prefix}_${kind}`;
 const reason=typeof error?.errno==='string'?error.errno:error?.code;
 const errno=typeof reason==='string'&&/^[A-Z0-9_]{1,80}$/.test(reason)?reason:undefined;
 return Object.assign(new Error(`${code}${committed?'：JSON 已提交；请读取确认，不要重复变更。':''}`),{code,committed,...(errno?{errno}:{})});
}

export function readJson(file,empty,validate,prefix){
 let text;
 try{
  // lstat distinguishes a missing file from a dangling symlink or a non-file target.
  const info=lstatSync(file);if(!info.isFile())throw persistenceError(prefix,'INVALID_DATA');
  text=readFileSync(file);
 }catch(error){
  if(error.code==='ENOENT'){
   // Only an absent leaf under an accessible real directory is an empty store.
   try{if(!statSync(dirname(file)).isDirectory())throw Error('not directory');lstatSync(file);}
   catch(check){if(check.code==='ENOENT'&&check.path===file)return empty();throw persistenceError(prefix,'READ_FAILED',check);}
   throw persistenceError(prefix,'READ_FAILED',error);
  }
  if(error.code?.startsWith(prefix+'_'))throw error;
  throw persistenceError(prefix,'READ_FAILED',error);
 }
 try{const value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(text));if(!validate(value))throw Error('schema');return value;}
 catch(error){throw persistenceError(prefix,'INVALID_DATA');}
}

export function atomicText(file,text,prefix){
 const temporary=join(dirname(file),'.'+randomUUID()+'.tmp');let fd,owned=false,committed=false;
 try{
  fd=openSync(temporary,'wx',0o600);owned=true;writeFileSync(fd,text,'utf8');fsyncSync(fd);closeSync(fd);fd=undefined;
  renameSync(temporary,file);owned=false;committed=true;
  const directory=openSync(dirname(file),'r');try{fsyncSync(directory);}finally{closeSync(directory);}
 }catch(error){throw persistenceError(prefix,'WRITE_FAILED',error,committed);}
 finally{
  if(fd!==undefined){try{closeSync(fd);}catch(closeError){/* Preserve the publication error. */}}
  if(owned){try{unlinkSync(temporary);}catch(cleanupError){/* Only the exclusive temporary belongs to this writer. */}}
 }
}

export function writerSync(directory,prefix,action){
 let actionFailed=false,finished=false;
 try{if(!statSync(directory).isDirectory())throw Error('not directory');return withStoreWriter(directory,()=>{try{const result=action();finished=true;return result;}catch(error){actionFailed=true;throw error;}});}
 catch(error){if(actionFailed||error.code?.startsWith(prefix+'_'))throw error;throw persistenceError(prefix,'WRITE_FAILED',error,finished);}
}

// Queue by canonical directory, including separate scheduler instances in the same process.
const tails=new Map();
export async function writerAsync(directory,prefix,action){
 let key;
 try{if(!statSync(directory).isDirectory())throw Error('not directory');key=realpathSync(directory);}
 catch(error){throw persistenceError(prefix,'WRITE_FAILED',error);}
 const prior=tails.get(key)??Promise.resolve();
 const run=prior.then(async()=>{
  let db,locked=false;
  try{
   db=new DatabaseSync(join(key,'.writer.sqlite'));const deadline=Date.now()+3000;
   for(;;){
    try{db.exec('BEGIN IMMEDIATE');locked=true;break;}
    catch(error){if(![5,6].includes(error.errcode)||Date.now()>=deadline)throw error;await delay(10);}
   }
   // No business records live in the lock DB; close rolls back the exclusion transaction.
   return await action();
  }catch(error){if(locked||error.code?.startsWith(prefix+'_'))throw error;throw persistenceError(prefix,'WRITE_FAILED',error);}
  finally{db?.close();}
 });
 const tail=run.then(()=>undefined,()=>undefined);tails.set(key,tail);
 return run.finally(()=>{if(tails.get(key)===tail)tails.delete(key);});
}
