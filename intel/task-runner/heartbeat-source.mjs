import {execFile} from 'node:child_process';
import {open,lstat,constants} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {isUtf8} from 'node:buffer';
import {join} from 'node:path';
import {storedRows} from '../../intel-plugins/dsh-intelligence-joblog/src/history.js';

const fail=code=>Object.assign(Error(code),{code});
const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const items=text=>text.split(/\r?\n/).map(x=>x.trim()).filter(x=>x&&!x.startsWith('#')&&!x.startsWith('_')).map(x=>x.replace(/^[-*]\s+/,''));
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);

/**
 * Validate deployment-owned health checks and bounds before reading any source.
 * @param config - Optional heartbeat config; absence retains legacy model dispatch.
 * @returns Enabled configuration or null for an explicitly disabled/older deployment.
 */
export function heartbeatConfig(config){
  if(config===undefined)return null;
  if(!object(config)||typeof config.enabled!=='boolean')throw fail('HEARTBEAT_INVALID_CONFIG');
  if(!config.enabled)return null;
  for(const [key,max] of Object.entries({sourceTimeoutMs:30000,maxSourceBytes:4194304,windowMs:86400000,pendingAfterMs:86400000,initialBackoffMs:86400000,maxBackoffMs:86400000,minDiskBytes:Number.MAX_SAFE_INTEGER,minMemoryBytes:Number.MAX_SAFE_INTEGER,listenPort:65535}))
    if(!Number.isSafeInteger(config[key])||config[key]<1||config[key]>max)throw fail('HEARTBEAT_INVALID_CONFIG');
  if(config.maxBackoffMs<config.initialBackoffMs||typeof config.serviceUnit!=='string'||!/^[a-zA-Z0-9@_.-]+\.service$/.test(config.serviceUnit)||
    typeof config.listenAddress!=='string'||!/^[a-zA-Z0-9:.]+$/.test(config.listenAddress))throw fail('HEARTBEAT_INVALID_CONFIG');
  return config;
}

async function boundedFile(file,maxBytes,{optional=false}={}){
  let handle;
  try{
    handle=await open(file,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);const before=await handle.stat();
    if(!before.isFile()||before.size>maxBytes)throw fail('HEARTBEAT_SOURCE_TOO_LARGE');
    const buffer=Buffer.alloc(maxBytes+1);let size=0;
    while(size<buffer.length){const part=await handle.read(buffer,size,buffer.length-size,null);if(!part.bytesRead)break;size+=part.bytesRead;}
    if(size>maxBytes)throw fail('HEARTBEAT_SOURCE_TOO_LARGE');
    const after=await handle.stat();
    if(before.size!==size||after.size!==size||before.mtimeMs!==after.mtimeMs)throw fail('HEARTBEAT_SOURCE_CHANGED');
    const bytes=buffer.subarray(0,size);if(!isUtf8(bytes))throw fail('HEARTBEAT_INVALID_SOURCE');
    return bytes.toString('utf8');
  }catch(error){if(optional&&error.code==='ENOENT')return null;if(error.code?.startsWith('HEARTBEAT_'))throw error;throw fail('HEARTBEAT_SOURCE_READ_FAILED');}
  finally{await handle?.close();}
}

/**
 * Execute one fixed read-only Linux probe with output and elapsed-time limits.
 * @param command - Fixed executable name resolved by the runner's existing PATH.
 * @param args - Explicit probe arguments, never a shell expression.
 * @param config - Source timeout and byte limit.
 * @returns Complete stdout; nonzero exits, timeout and truncation reject.
 */
export function executeHeartbeatProbe(command,args,config){
  return new Promise((resolve,reject)=>execFile(command,args,{encoding:'utf8',timeout:config.sourceTimeoutMs,maxBuffer:config.maxSourceBytes,killSignal:'SIGKILL',env:{...process.env,LC_ALL:'C'}},(error,stdout)=>{
    if(error){reject(fail(error.code==='ERR_CHILD_PROCESS_STDIO_MAXBUFFER'?'HEARTBEAT_SOURCE_TOO_LARGE':error.killed?'HEARTBEAT_SOURCE_TIMEOUT':'HEARTBEAT_PROBE_FAILED'));return;}
    resolve(stdout);
  }));
}

function json(value){try{return JSON.parse(value);}catch{throw fail('HEARTBEAT_INVALID_SOURCE');}}
function dateMs(value){const ms=Date.parse(value);if(!Number.isSafeInteger(ms))throw fail('HEARTBEAT_INVALID_SOURCE');return ms;}
const safeId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
function cronSignals(text,latest,history,nowMs,config,scope){
  if(text&&!text.endsWith('\n'))throw fail('HEARTBEAT_SOURCE_INCOMPLETE');
  const active=new Map(),failures=new Map();
  for(const line of text.split('\n').filter(Boolean)){
    const row=json(line);
    if(!object(row)||typeof row.kind!=='string'||!safeId(row.taskId)||typeof row.time!=='string'||
      (row.scope!==undefined&&!['production','test'].includes(row.scope)))throw fail('HEARTBEAT_INVALID_SOURCE');
    const time=dateMs(row.time);
    if(row.scope!==undefined&&row.scope!==scope)continue;
    // Old rows keep an explicit unclassified identity; no task-name inference selects their source.
    const origin=row.scope??'unclassified';
    if(row.kind==='start'||row.kind==='finish'){
      if(row.taskId==='heartbeat')continue; // Its pending state owns failed/uncertain heartbeat execution; avoid self-trigger loops.
      if(time>nowMs)throw fail('HEARTBEAT_SOURCE_TIME_FUTURE');
      if(row.runId===undefined&&time<nowMs-config.windowMs)continue;
      if(row.runId===undefined)throw fail('HEARTBEAT_LEGACY_RUN_UNVERIFIED');
      if(!safeId(row.runId)||(row.kind==='finish'&&!Number.isInteger(row.exit)))throw fail('HEARTBEAT_INVALID_SOURCE');
      const key=JSON.stringify([origin,row.taskId,row.runId]);
      if(row.kind==='start'){if(active.has(key))throw fail('HEARTBEAT_INVALID_SOURCE');active.set(key,{source:'route',scope:origin,taskId:row.taskId,runId:row.runId,timeMs:time,status:'unfinished'});}
      else{active.delete(key);if(row.exit!==0&&time>=nowMs-config.windowMs)failures.set(key,{source:'route',scope:origin,taskId:row.taskId,runId:row.runId,timeMs:time,status:'failed',exit:row.exit});}
    }
  }
  if(!object(latest))throw fail('HEARTBEAT_INVALID_SOURCE');
  const rows=[...Object.entries(latest).map(([id,row])=>({...row,id})),...history];
  for(const row of rows){
    if(!object(row)||!safeId(row.id)||typeof row.last!=='string'||!['ok','fail'].includes(row.status)||
      (row.scope!==undefined&&!['production','test','unclassified'].includes(row.scope)))throw fail('HEARTBEAT_INVALID_SOURCE');
    const time=dateMs(row.last),origin=row.scope??'unclassified';
    if(origin!=='unclassified'&&origin!==scope)continue;
    if(row.id==='heartbeat')continue;
    if(time>nowMs)throw fail('HEARTBEAT_SOURCE_TIME_FUTURE');
    if(row.status==='fail'&&time>=nowMs-config.windowMs){
      if(row.runId!==undefined&&!safeId(row.runId))throw fail('HEARTBEAT_INVALID_SOURCE');
      const key=JSON.stringify([origin,row.id,row.runId??row.last]);
      if(!failures.has(key))failures.set(key,{source:'joblog',scope:origin,taskId:row.id,...row.runId?{runId:row.runId}:{},timeMs:time,status:'failed'});
    }
  }
  return [...failures.values(),...[...active.values()].filter(row=>nowMs-row.timeMs>config.pendingAfterMs)].sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

function alertSignals(text,nowMs,config,scope){
  if(text===null||!text.trim())return {id:hash([]),events:[]};
  if(!text.endsWith('\n'))throw fail('HEARTBEAT_SOURCE_INCOMPLETE');
  const chunks=text.split(/(?=^## )/m),selected=[],events=[];
  for(const chunk of chunks){
    if(!chunk.trim())continue;
    const match=/^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^\r\n\]]+)\] 运行失败(?: \{scope:(production|test)\})?\r?\n/.exec(chunk);
    if(!match||!safeId(match[2]))throw fail('HEARTBEAT_INVALID_SOURCE');
    const origin=match[3]??'unclassified';if(origin!=='unclassified'&&origin!==scope||match[2]==='heartbeat')continue;
    // Legacy alert timestamps retain the Host-local clock basis; historical bodies only enter an opaque hash.
    const timeMs=dateMs(match[1]),id=hash(chunk.trimEnd());selected.push(id);
    if(timeMs>nowMs)throw fail('HEARTBEAT_SOURCE_TIME_FUTURE');
    if(timeMs>=nowMs-config.windowMs)events.push({source:'alert',scope:origin,taskId:match[2],id,timeMs,timeBasis:'host-local',status:'failed'});
  }
  return {id:hash(selected),events};
}

async function historyRows(file,maxBytes){
  let info;try{info=await lstat(file);}catch(error){if(error.code==='ENOENT')return [];throw fail('JOBLOG_READ_FAILED');}
  if(!info.isFile()||info.size>maxBytes)throw fail('JOBLOG_INVALID_DATA');
  return storedRows(file,'runs');
}

/**
 * Capture the shipped checklist's six health sources without model execution or data writes.
 * Empty existing checklists are explicit opt-outs; unfamiliar nonempty lists cannot establish health.
 * @param options - Home, validated config, observation time, explicit scope and optional probe adapter.
 * @returns Complete safe signals with hashed source identities; raw journal text is never returned.
 */
export async function captureHeartbeat({home,config,nowMs,scope='production',probe=executeHeartbeatProbe}){
  if(!Number.isSafeInteger(nowMs)||nowMs<0||!['production','test'].includes(scope))throw fail('HEARTBEAT_INVALID_CONFIG');
  const checklist=items(await boundedFile(join(home,'intel-evolution','HEARTBEAT.md'),config.maxSourceBytes));
  const checklistId=hash(checklist);
  if(!checklist.length)return {version:1,complete:true,observedAtMs:nowMs,checklistId,empty:true,healthy:true,signals:[],signalId:hash([scope,checklistId,'empty'])};
  const canonical=items(await boundedFile(new URL('../../intel-plugins/dsh-intelligence-evolution/HEARTBEAT.md',import.meta.url),config.maxSourceBytes));
  if(checklistId!==hash(canonical))throw fail('HEARTBEAT_UNSUPPORTED_CHECKLIST');
  const root=join(home,'intel-joblog');
  const reads=await Promise.allSettled([
    probe('systemctl',['show',config.serviceUnit,'--property=ActiveState,SubState,NRestarts,InvocationID'],config),
    probe('ss',['-H','-ltn'],config),probe('df',['-Pk','/'],config),probe('free',['-b'],config),
    probe('journalctl',['-u',config.serviceUnit,'--since','@'+((nowMs-config.windowMs)/1000).toFixed(3),'--until','@'+(nowMs/1000).toFixed(3),'-p','err','-o','json','--no-pager','--quiet'],config),
    boundedFile(join(root,'route-runs.jsonl'),config.maxSourceBytes),boundedFile(join(root,scope==='test'?'job_test_runs.json':'job_runs.json'),config.maxSourceBytes),
    boundedFile(join(root,'job_alerts.md'),config.maxSourceBytes,{optional:true}),historyRows(join(root,'job_history.sqlite'),config.maxSourceBytes),
  ]);
  const sources=['service','listener','disk','memory','journal','route','joblog','joblog','joblog'];
  for(const [i,r] of reads.entries())if(r.status==='rejected')throw Object.assign(r.reason,{source:sources[i]});
  const [service,listeners,disk,memory,journal,routes,latest,alertText,history]=reads.map(r=>r.value);
  const fields=Object.fromEntries(service.trim().split('\n').map(line=>line.split('=')));
  if(!['ActiveState','SubState','NRestarts','InvocationID'].every(k=>typeof fields[k]==='string')||!/^[0-9]+$/.test(fields.NRestarts)||
    !/^[a-z-]+$/.test(fields.ActiveState)||!/^[a-z-]+$/.test(fields.SubState)||!/^([a-f0-9]{1,64})?$/.test(fields.InvocationID))throw fail('HEARTBEAT_INVALID_SOURCE');
  const listenLines=listeners.trim()?listeners.trim().split('\n').map(x=>x.trim().split(/\s+/)):[];
  if(listenLines.some(x=>x.length<5||x[0]!=='LISTEN'||!x[3].includes(':')))throw fail('HEARTBEAT_INVALID_SOURCE');
  const address=config.listenAddress.includes(':')?'['+config.listenAddress+']':config.listenAddress;
  const listening=listenLines.some(x=>x[3]===address+':'+config.listenPort);
  const df=disk.trim().split('\n');const cols=df[1]?.trim().split(/\s+/);
  if(df.length!==2||!cols||cols.length!==6||!cols.slice(1,4).every(x=>/^\d+$/.test(x))||cols[5]!=='/')throw fail('HEARTBEAT_INVALID_SOURCE');
  const availableDisk=Number(cols[3])*1024;
  const mem=memory.split('\n').find(x=>x.startsWith('Mem:'))?.trim().split(/\s+/);
  if(!mem||mem.length!==7||!mem.slice(1).every(x=>/^\d+$/.test(x)))throw fail('HEARTBEAT_INVALID_SOURCE');
  const availableMemory=Number(mem[6]);
  if(!Number.isSafeInteger(availableDisk)||!Number.isSafeInteger(availableMemory))throw fail('HEARTBEAT_INVALID_SOURCE');
  if(journal&&!journal.endsWith('\n'))throw fail('HEARTBEAT_SOURCE_INCOMPLETE');
  const errors=journal.split('\n').filter(Boolean).map(line=>{
    const row=json(line);
    if(!object(row)||typeof row.__CURSOR!=='string'||!row.__CURSOR||row.__CURSOR.length>4096||typeof row.__REALTIME_TIMESTAMP!=='string'||!/^\d+$/.test(row.__REALTIME_TIMESTAMP))throw fail('HEARTBEAT_INVALID_SOURCE');
    const timeMs=Math.floor(Number(row.__REALTIME_TIMESTAMP)/1000);
    if(!Number.isSafeInteger(timeMs)||timeMs<nowMs-config.windowMs||timeMs>nowMs)throw fail('HEARTBEAT_INVALID_SOURCE');
    return {id:hash(row.__CURSOR),timeMs};
  });
  const alerts=alertSignals(alertText,nowMs,config,scope);
  const cron=[...cronSignals(routes,json(latest),history,nowMs,config,scope),...alerts.events];
  const signals=[
    {source:'service',status:fields.ActiveState==='active'&&fields.SubState==='running'&&Number(fields.NRestarts)===0?'healthy':'anomaly',id:hash(fields)},
    {source:'listener',status:listening?'healthy':'anomaly',id:hash([address,config.listenPort,listening])},
    {source:'disk',status:availableDisk>config.minDiskBytes?'healthy':'anomaly',id:hash(['disk',availableDisk>config.minDiskBytes])},
    {source:'memory',status:availableMemory>config.minMemoryBytes?'healthy':'anomaly',id:hash(['memory',availableMemory>config.minMemoryBytes])},
    {source:'journal',status:errors.length?'anomaly':'healthy',id:hash(errors),events:errors},
    {source:'cron',status:cron.length?'anomaly':'healthy',id:hash([cron,alerts.id]),events:cron},
  ];
  return {version:1,complete:true,observedAtMs:nowMs,checklistId,empty:false,healthy:signals.every(x=>x.status==='healthy'),signals,
    signalId:hash([scope,checklistId,config,signals])};
}
