import {openSync,readSync,fstatSync,closeSync,constants} from 'node:fs';
import {resolve,isAbsolute} from 'node:path';
import {pathToFileURL} from 'node:url';

const quietKinds=new Set(['heartbeat-empty-checklist','heartbeat-no-change','heartbeat-healthy','heartbeat-backoff','upkeep-backoff','upkeep-no-signal','upkeep-bootstrap','upkeep-clock-rollback']);
const skippedKinds=new Set(['heartbeat-circuit-open','heartbeat-circuit-unavailable','circuit-open']);
const controlKinds=new Set(['circuit-released','upkeep-recovered']);
const safeCode=value=>typeof value==='string'&&/^[A-Z0-9_]{1,80}$/.test(value)?value:'UNKNOWN';
const identity=value=>typeof value==='string'&&/^[-a-zA-Z0-9._:]{1,256}$/.test(value);
const calendarDate=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;

/**
 * Pair one day's prompt-free audit by task/run/date, retaining incomplete and ambiguous evidence.
 * @param rows - Parsed route audit records; absent scope remains unclassified.
 * @param date - Explicit ISO calendar date in the audit's runDate convention.
 * @returns Process-outcome counts and identities; neither tool success nor output quality is inferred.
 */
export function measureRuns(rows,date){
 if(!calendarDate(date)||!Array.isArray(rows))throw Error('MEASURE_INVALID_INPUT');
 const groups=new Map(),observations={quiet:{},skipped:{},control:{}},counts={completed:0,succeeded:0,failed:0,reused:0,pending:0,orphan:0,ambiguous:0,legacyUnpaired:0,quiet:0,skipped:0,control:0,explicitTestRows:0};
 let zeroModelReceipts=0,undatedRows=0;
 for(const row of rows){
  if(!row||typeof row!=='object'||Array.isArray(row))throw Error('MEASURE_INVALID_RECORD');
  if(row.runDate!==undefined&&!calendarDate(row.runDate))throw Error('MEASURE_INVALID_RECORD');
  const category=quietKinds.has(row.kind)?'quiet':skippedKinds.has(row.kind)?'skipped':controlKinds.has(row.kind)?'control':undefined;
  const observedDay=category&&typeof row.time==='string'&&Number.isFinite(Date.parse(row.time))?
   new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(row.time)):undefined;
  const selectedDate=row.runDate??observedDay;
  if(selectedDate===undefined){undatedRows++;continue;}
  if(selectedDate!==date)continue;
  if(row.scope==='test'){counts.explicitTestRows++;continue;}
  if(category){
   if(row.scope!==undefined&&row.scope!=='production'||!identity(row.taskId)||row.kind.startsWith('heartbeat-')&&row.taskId!=='heartbeat'||row.kind.startsWith('upkeep-')&&!['evolve_upkeep','memory_upkeep'].includes(row.taskId)||row.modelExecutions!==undefined&&row.modelExecutions!==0)throw Error('MEASURE_INVALID_RECORD');
   counts[category]++;observations[category][row.kind]=(observations[category][row.kind]??0)+1;
   if(row.modelExecutions===0)zeroModelReceipts++;
   continue;
  }
  if(!['start','finish','already-published'].includes(row.kind))continue;
  if(row.runId!==undefined&&!identity(row.runId)||row.taskId!==undefined&&!identity(row.taskId))throw Error('MEASURE_INVALID_RECORD');
  if(!identity(row.runId)||!identity(row.taskId)){counts.legacyUnpaired++;continue;}
  const key=JSON.stringify([row.taskId,row.runId,row.runDate]);
  if(!groups.has(key))groups.set(key,{taskId:row.taskId,runId:row.runId,runDate:date,starts:[],finishes:[],reuse:[],scopes:new Set()});
  const group=groups.get(key);group.scopes.add(row.scope==='production'?'production':row.scope===undefined?'unclassified':'invalid');
  group[row.kind==='start'?'starts':row.kind==='finish'?'finishes':'reuse'].push(row);
 }
 const runs=[];
 for(const g of groups.values()){
  const r={taskId:g.taskId,runId:g.runId,runDate:g.runDate,scope:g.scopes.size===1?[...g.scopes][0]:'ambiguous'};
  const f=g.finishes[0];
  if(g.starts.length>1||g.finishes.length>1||g.reuse.length>1||g.scopes.size!==1||g.scopes.has('invalid')||f&&(!Number.isSafeInteger(f.exit)||f.exit<0||f.exit>255||f.exit===0&&f.processExit!==undefined&&f.processExit!==0||f.reused!==undefined&&typeof f.reused!=='boolean'||f.reused===true&&(!identity(g.reuse[0]?.publishedRunId)||g.reuse[0].publishedRunId===g.runId)||g.reuse.length&&f.reused!==true))r.status='ambiguous';
  else if(!g.starts.length)r.status='orphan';
  else if(!f)r.status='pending';
  else{
   counts.completed++;r.exit=f.exit;
   if(Number.isSafeInteger(f.processExit))r.processExit=f.processExit;
   r.status=f.exit!==0?'failed':f.reused===true?'reused':'succeeded';
   if(f.exit!==0)r.code=safeCode(f.code);
   if(r.status==='reused'&&identity(g.reuse[0]?.publishedRunId))r.publishedRunId=g.reuse[0].publishedRunId;
  }
  counts[r.status]++;runs.push(r);
 }
 return {version:1,date,counts,...observations,zeroModelReceipts,undatedRows,runs,
  limitations:['Retained audit only; undated legacy runs and unfamiliar kinds are not assigned a date. Known observations use runDate or their audit time in Asia/Shanghai.','Process success is not verified tool success, publication quality or provider billing.','Quiet, skipped and control observations are not completed model runs; only explicit modelExecutions=0 is counted as a zero-model receipt.']};
}

/** Read a bounded regular audit file without creating state or following a final symlink. */
function main(){
 const [file,date]=process.argv.slice(2);
 if(!file||!isAbsolute(file)||!date||process.argv.length!==4)throw Error('Usage: node intel/task-runner/measure.mjs /absolute/route-runs.jsonl YYYY-MM-DD');
 const fd=openSync(file,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
 try{
  const stat=fstatSync(fd);if(!stat.isFile()||stat.size>16*1024*1024)throw Error('MEASURE_SOURCE_LIMIT');
  const chunks=[],limit=16*1024*1024;let total=0;
  for(;;){
   const buffer=Buffer.alloc(Math.min(65536,limit-total+1)),count=readSync(fd,buffer,0,buffer.length,null);
   if(!count)break;total+=count;if(total>limit)throw Error('MEASURE_SOURCE_LIMIT');chunks.push(buffer.subarray(0,count));
  }
  const text=new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(chunks)),after=fstatSync(fd);
  if(text&&!text.endsWith('\n')||after.size!==stat.size||after.mtimeMs!==stat.mtimeMs||total!==stat.size)throw Error('MEASURE_INCOMPLETE_SOURCE');
  const rows=text.split('\n').filter(Boolean).map(line=>JSON.parse(line));
  console.log(JSON.stringify(measureRuns(rows,date),null,2));
 }finally{closeSync(fd);}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 try{main();}catch{console.error('MEASURE_SOURCE_FAILED');process.exitCode=1;}
}
