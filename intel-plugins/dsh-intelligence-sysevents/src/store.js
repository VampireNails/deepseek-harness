// Structured system events share the existing HMC JSONL source.
import {mkdirSync,readFileSync,openSync,writeSync,fsyncSync,closeSync,statSync} from 'node:fs';
import {join} from 'node:path';
import {homedir} from 'node:os';
import {randomUUID,createHash} from 'node:crypto';
import {withStoreWriter} from '../../shared/persistence.js';

const MAX_SOURCE_BYTES=16*1024*1024;
export const SEVERITIES=Object.freeze({low:'low',normal:'normal',high:'high'});
const fault=code=>Object.assign(Error(code),{code});

/** Resolve the shared DSH home without creating files during a read. */
export function sysEventsDir(){return join(process.env.DSH_HOME??join(homedir(),'.dsh'),'intel-system-events');}

function fileFor(dir,scope){return join(dir,scope==='test'?'events.test.jsonl':'events.jsonl');}
function readSource(file){
  let text;
  try{
    if(statSync(file).size>MAX_SOURCE_BYTES)throw fault('SYSEVENTS_SOURCE_TOO_LARGE');
    text=readFileSync(file,'utf8');
  }catch(error){if(error.code==='ENOENT')return [];if(error.code?.startsWith('SYSEVENTS_'))throw error;throw fault('SYSEVENTS_READ_FAILED');}
  if(text&&!text.endsWith('\n'))throw fault('SYSEVENTS_INVALID_DATA');
  return text.split('\n').filter(Boolean).map(line=>{
    let value;try{value=JSON.parse(line);}catch(error){throw fault('SYSEVENTS_INVALID_DATA');}
    if(!value||typeof value!=='object'||typeof value.id!=='string'||!value.id||typeof value.ts!=='string'||
      !Number.isFinite(Date.parse(value.ts))||new Date(value.ts).toISOString()!==value.ts||
      !Object.hasOwn(SEVERITIES,value.severity)||typeof value.title!=='string'||typeof value.type!=='string'||
      (value.scope!==undefined&&!['production','test'].includes(value.scope)))throw fault('SYSEVENTS_INVALID_DATA');
    return value;
  });
}

/**
 * Append under the shared writer lock; identical structured reports return the first event.
 * @param entry - Public fields and optional task/run/source identity; raw error objects are excluded.
 * @param dir - Event directory in the shared DSH home.
 * @returns The durable event or existing report; IO and identity conflicts throw safe codes.
 */
export function emitEvent(entry,dir=sysEventsDir()){
  const {type,severity,title,detail}=entry??{},scope=entry?.scope??process.env.INTEL_JOBLOG_SCOPE??'production';
  if(typeof type!=='string'||!type.trim()||typeof title!=='string'||!title.trim()||!['production','test'].includes(scope))throw fault('SYSEVENTS_INVALID_ENTRY');
  const fields={type:type.trim().slice(0,128),severity:Object.hasOwn(SEVERITIES,severity)?severity:'normal',title:title.trim().slice(0,256),detail:typeof detail==='string'?detail.trim().slice(0,2000):'',scope};
  for(const key of ['source','taskId','runId','sessionId','scheduleId','exitCategory','code']){
    if(entry[key]===undefined)continue;
    if(typeof entry[key]!=='string'||!/^[a-zA-Z0-9_.:-]{1,128}$/.test(entry[key]))throw fault('SYSEVENTS_INVALID_ENTRY');
    fields[key]=entry[key];
  }
  if(entry.exitCode!==undefined){if(!Number.isSafeInteger(entry.exitCode))throw fault('SYSEVENTS_INVALID_ENTRY');fields.exitCode=entry.exitCode;}
  const structured=fields.runId!==undefined;
  if(structured&&(!fields.taskId||!fields.source||!fields.code))throw fault('SYSEVENTS_INVALID_ENTRY');
  const id=structured?createHash('sha256').update(JSON.stringify([scope,fields.type,fields.source,fields.taskId,fields.runId,fields.sessionId??null])).digest('hex'):randomUUID();
  try{
    mkdirSync(dir,{recursive:true,mode:0o700});
    return withStoreWriter(dir,()=>{
      const file=fileFor(dir,scope),previous=readSource(file).find(value=>value.id===id);
      if(previous){
        const {id:oldId,ts,...old}=previous;if(JSON.stringify(old)!==JSON.stringify(fields))throw fault('SYSEVENTS_IDENTITY_CONFLICT');
        // A previous failed fsync may already have appended this identity; retry its barrier.
        const fd=openSync(file,'r');try{fsyncSync(fd);}finally{closeSync(fd);}return previous;
      }
      const event={id,ts:new Date().toISOString(),...fields},fd=openSync(file,'a',0o600);
      try{const buffer=Buffer.from(JSON.stringify(event)+'\n');let offset=0;while(offset<buffer.length)offset+=writeSync(fd,buffer,offset);fsyncSync(fd);}finally{closeSync(fd);}
      return event;
    });
  }catch(error){if(error.code?.startsWith('SYSEVENTS_'))throw error;throw fault('SYSEVENTS_WRITE_FAILED');}
}

/**
 * Missing files are empty; damaged sources reject instead of hiding events.
 * @param options - Inclusive time cursor, limit and explicit includeTests flag.
 * @param dir - Event directory.
 * @returns At most 200 events; explicit tests are excluded by default.
 */
export function listEvents({since=null,limit=50,includeTests=false}={},dir=sysEventsDir()){
  const out=readSource(fileFor(dir,'production')).filter(value=>value.scope!=='test');
  if(includeTests)out.push(...readSource(fileFor(dir,'test')));
  return out.filter(value=>!since||value.ts>=since).sort((a,b)=>a.ts.localeCompare(b.ts)).slice(-Math.max(1,Math.min(limit,200)));
}
