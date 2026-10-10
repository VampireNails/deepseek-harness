// store.js —— job 元数据持久化：DSH_HOME/intel-cron/cron.json（对标 Python 的 memory/custom_cron.json）。
//
// 写入顺序保证 crash 安全：
//   - 创建：先落盘 job 记录，再 append schedule/change。crash 在中间 → 对账补建（只防丢）。
//   - 删除：先从文件删 job，再 append schedule delete。crash 在中间 → 孤儿 schedule
//     会在对账时被回收（只触发一次，不复活）。

import { mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";
import {readJson,atomicText,writerAsync,persistenceError} from '../../shared/json-store.js';

const record=value=>value&&typeof value==='object'&&!Array.isArray(value);
function validJobs(jobs){
 if(!Array.isArray(jobs))return false;
 const ids=new Set();
 return jobs.every(j=>{
  if(!record(j)||!/^c[1-9]\d*$/.test(j.id)||!Number.isSafeInteger(Number(j.id.slice(1)))||ids.has(j.id)||
   typeof j.name!=='string'||!j.name||typeof j.prompt!=='string'||!j.prompt||typeof j.sessionId!=='string'||!j.sessionId||
   !record(j.schedule)||typeof j.scheduleId!=='string'||!(j.scheduleId===`cron-${j.id}`||new RegExp(`^cron-${j.id}-\\d{8}$`).test(j.scheduleId))||
   typeof j.nextFire!=='string'||!Number.isFinite(Date.parse(j.nextFire))||('done'in j&&typeof j.done!=='boolean'))return false;
  ids.add(j.id);const s=j.schedule;
  if(s.kind==='interval'||s.kind==='after')return Number.isSafeInteger(s.seconds)&&s.seconds>=(s.kind==='interval'?300:1);
  if(!['daily','weekly','once'].includes(s.kind)||!Number.isInteger(s.hour)||s.hour<0||s.hour>23||!Number.isInteger(s.minute)||s.minute<0||s.minute>59)return false;
  return s.kind!=='weekly'||(Number.isInteger(s.dayOfWeek)&&s.dayOfWeek>=1&&s.dayOfWeek<=7);
 });
}
const maxId=jobs=>jobs.reduce((max,j)=>Math.max(max,Number(j.id.slice(1))),0);
function validDocument(data){return Array.isArray(data)?validJobs(data):record(data)&&data.version===1&&validJobs(data.jobs)&&Number.isSafeInteger(data.seq)&&data.seq>=maxId(data.jobs);}

export function defaultDataDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-cron");
}

export class CronStore {
  constructor(dataDir) {
    this.file = join(dataDir || defaultDataDir(), "cron.json");
    try{mkdirSync(dirname(this.file),{recursive:true});}catch(error){throw persistenceError('CRON','WRITE_FAILED',error);}
  }

  load() {
    return this._document().jobs;
  }

  _document(){
    const data=readJson(this.file,()=>[],validDocument,'CRON');
    return Array.isArray(data)?{version:1,seq:maxId(data),jobs:data}:data;
  }

  nextId(seen){
    const data=this._document();let seq=data.seq;
    for(const id of seen){const match=/^cron-c(\d+)(?:-|$)/.exec(id);if(match)seq=Math.max(seq,Number(match[1]));}
    if(!Number.isSafeInteger(seq+1))throw persistenceError('CRON','ID_EXHAUSTED');
    return nextJobId(data.jobs,seq);
  }

  transaction(action){
    return writerAsync(dirname(this.file),'CRON',action);
  }

  save(jobs) {
    const old=this._document();
    if(!validJobs(jobs))throw persistenceError('CRON','INVALID_DATA');
    atomicText(this.file,JSON.stringify({...old,seq:Math.max(old.seq,maxId(jobs)),jobs},null,2),'CRON');
  }
}

/** 分配下一个 cN id（对标 Python _next_id）。 */
export function nextJobId(jobs,seq=0) {
  let max = seq;
  for (const j of jobs) {
    const m = /^c(\d+)$/.exec(String(j.id || ""));
    if (m) max = Math.max(max, parseInt(m[1], 10));
  }
  return `c${max + 1}`;
}
