import { appendFileSync, mkdirSync, readFileSync, writeFileSync, existsSync,openSync,closeSync,fstatSync,constants } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import {randomUUID} from 'node:crypto';
import {atomicWrite,persistRun,storedRows,storageError} from './history.js';

export function joblogDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-joblog");
}

const MAX_LOG_LINES = 2000;
function diagnostic(value){return String(value??'').replace(/((?:prompt|password|api[_-]?key|token|authorization|secret)\s*[:=]\s*)[^\r\n]+/gi,'$1[redacted]').replace(/Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+/g,'[redacted]');}

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 后台任务的运行日志与失败告警（复刻 Python 版 joblog 三件套）。
// 不依赖 dsh，可被 cron 包装脚本直接 require。
export class JobLog {
  constructor(dir = joblogDir(), { scope = 'production', historyLimit = 200 } = {}) {
    if (!['production', 'test'].includes(scope)) throw new Error('Invalid JobLog scope');
    if(!Number.isInteger(historyLimit)||historyLimit<1||historyLimit>1000)throw Error('Invalid JobLog historyLimit');
    this.historyLimit=historyLimit;
    this.scope = scope;
    this.dir = dir;
    this.logFile = join(dir, "jobs.log");
    this.runsFile = join(dir, "job_runs.json");
    this.testRunsFile = join(dir, "job_test_runs.json");
    this.alertsFile = join(dir, "job_alerts.md");
    this.historyFile=join(dir,'job_history.sqlite');
    mkdirSync(dir, { recursive: true });
  }

  // 包装一个任务：记日志、更新运行状态、失败告警
  async guarded(jobId, fn) {
    const identity={runId:randomUUID(),startedAt:new Date().toISOString()};
    this._log(`[${ts()}] ${jobId} 开始 runId=${identity.runId}`);
    let result,failure,failed=false;
    try {
      result = await fn();
    } catch (e) {
      failure=e;failed=true;
    }
    const status=failed?'fail':'ok';
    // Arbitrary task result/error bodies can contain prompts and secrets. Persist only safe classification.
    const detail=status==='ok'?'completed':`task failed${typeof failure?.code==='string'&&/^[A-Z0-9_]{1,80}$/.test(failure.code)?'; code '+failure.code:''}; runId=${identity.runId}`;
    try{
      this._recordRun(jobId,status,detail,identity);
      if(status==='fail')this._alert(jobId,detail);
      this._log(`[${ts()}] ${jobId} ${status==='ok'?'成功':'失败'} runId=${identity.runId}`);
    }catch(error){
      if(failed)throw Object.assign(storageError('JOBLOG_PERSISTENCE_FAILED',error.operation,error),{taskStatus:'fail',taskError:failure,persistenceCode:error.code});
      throw error;
    }
    if(failed)throw failure;
    return result;
  }

  /**
   * Read latest states; unknown history stays visible.
   * @param options - includeTests also selects explicitly marked test records.
   * @returns Selected records without changing the stored projection.
   */
  readRuns({ includeTests = false } = {}) {
    const runs = this._readRunFile(this.runsFile);
    if(existsSync(this.historyFile))for(const [id,row]of Object.entries(runs))if(row.runId)delete runs[id];
    for(const row of storedRows(this.historyFile,'latest'))if(row.scope==='production')runs[row.id]=row;
    if (!includeTests) return Object.fromEntries(Object.entries(runs).filter(([,run]) => run.scope !== 'test'));
    const selected = {};
    for (const [id, run] of Object.entries(runs)) selected[JSON.stringify([run.scope ?? 'unclassified', id])] = { ...run, id };
    const tests=this._readRunFile(this.testRunsFile);
    if(existsSync(this.historyFile))for(const [id,row]of Object.entries(tests))if(row.runId)delete tests[id];
    for(const row of storedRows(this.historyFile,'latest'))if(row.scope==='test')tests[row.id]=row;
    for (const [id, run] of Object.entries(tests)) {
      if (run.scope !== 'test') throw Object.assign(new Error('JOBLOG_INVALID_DATA'), { code: 'JOBLOG_INVALID_DATA' });
      const key = JSON.stringify(['test', id]);
      if (Object.hasOwn(selected, key)) throw Object.assign(new Error('JOBLOG_SOURCE_COLLISION'), { code: 'JOBLOG_SOURCE_COLLISION' });
      selected[key] = { ...run, id };
    }
    return selected;
  }

  /** Read newest completions with legacy snapshots; default selects production only. */
  readHistory({includeTests=false,limit=100,jobId}={}){
    if(!Number.isInteger(limit)||limit<1||limit>1000)throw Error('Invalid JobLog limit');
    const rows=storedRows(this.historyFile,'runs');
    const legacy=new Map(storedRows(this.historyFile,'legacy').map(row=>[JSON.stringify([row.scope,row.id]),row]));
    const latest=includeTests?Object.values(this.readRuns({includeTests:true})):Object.entries(this.readRuns()).map(([id,row])=>({...row,id,scope:row.scope??'unclassified'}));
    for(const row of latest){
      if(!row.runId){
        const scope=row.scope??'unclassified',key=JSON.stringify([scope,row.id]);
        if(!rows.some(r=>r.id===row.id&&r.scope===scope))legacy.set(key,{...row,scope,detail:row.detail??'',legacy:true});
      }
    }
    const seen=new Set();
    const older=[...legacy.values()].filter(row=>{const key=JSON.stringify([row.scope,row.id,row.last,row.status,row.detail]);if(seen.has(key))return false;seen.add(key);return true;});
    return [...rows,...older].filter(row=>(includeTests||row.scope==='production')&&(jobId===undefined||row.id===jobId)).slice(0,limit);
  }

  _readRunFile(file) {
    let raw,fd;
    try {
      fd=openSync(file,constants.O_RDONLY|(constants.O_NOFOLLOW??0));
      const info=fstatSync(fd);
      if(!info.isFile())throw storageError('JOBLOG_READ_FAILED','latest');
      if(info.size>512*1024)throw storageError('JOBLOG_INVALID_DATA','latest');
      raw=readFileSync(fd,'utf8');
    }
    catch (error) {
      if (error.code === 'ENOENT') return {};
      if(error.code?.startsWith('JOBLOG_'))throw error;
      throw Object.assign(new Error('JOBLOG_READ_FAILED'), { code: 'JOBLOG_READ_FAILED' });
    }finally{if(fd!==undefined)closeSync(fd);}
    let runs;
    try { runs = JSON.parse(raw); }
    catch (error) { throw Object.assign(new Error('JOBLOG_INVALID_DATA'), { code: 'JOBLOG_INVALID_DATA' }); }
    if (!runs || typeof runs !== 'object' || Array.isArray(runs) || Object.values(runs).some(run =>
      !run || typeof run !== 'object' || Array.isArray(run) || typeof run.last !== 'string' ||
      typeof run.status !== 'string' || (run.detail !== undefined && typeof run.detail !== 'string') ||
      (run.scope !== undefined && !['production', 'test'].includes(run.scope))))
      throw Object.assign(new Error('JOBLOG_INVALID_DATA'), { code: 'JOBLOG_INVALID_DATA' });
    return runs;
  }

  /**
   * Select marked alerts without changing storage or guessing older sources.
   * @param text - The caller's alert text, which may already have a read size limit.
   * @param options - includeTests retains explicitly marked test blocks.
   * @returns Alert text retaining production and unmarked historical failures.
   */
  selectAlerts(text, { includeTests = false, productionOnly=false } = {}) {
    if (includeTests) return text;
    if(productionOnly)return (text.match(/\n## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[[^\r\n]*\] 运行失败 \{scope:production\}\n(?:    [^\n]*\n)*（完整记录见 jobs.log 或 route-runs.jsonl）\n/g)||[]).join('');
    // Indented details cannot introduce a scoped record header.
    return text.replace(/\n## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[[^\r\n]*\] 运行失败 \{scope:test\}\n(?:    [^\n]*\n)*（完整记录见 jobs.log 或 route-runs.jsonl）\n/g, '');
  }

  /**
   * Read selected failures, retaining unmarked history and original dates.
   * @param options - includeTests selects all stored failures.
   * @returns Selected text, or the empty-alert message when absent or empty.
   * @throws JOBLOG_READ_FAILED when the alert source cannot be read.
   */
  readAlerts(options = {}) {
    let text;
    try {
      text = readFileSync(this.alertsFile, "utf8");
    } catch (error) {
      if (error.code === 'ENOENT') return "(无告警)";
      throw Object.assign(new Error('JOBLOG_READ_FAILED'), { code: 'JOBLOG_READ_FAILED' });
    }
    return this.selectAlerts(text, options).trim() || "(无告警)";
  }

  _log(line) {
    try {
      appendFileSync(this.logFile, line + "\n");
      const lines = readFileSync(this.logFile, "utf8").split("\n");
      if (lines.length > MAX_LOG_LINES + 1) {
        writeFileSync(this.logFile, lines.slice(-MAX_LOG_LINES).join("\n"));
      }
    } catch(error) {throw storageError('JOBLOG_WRITE_FAILED','log',error);}
  }

  _recordRun(jobId, status, detail, {runId=randomUUID(),startedAt,finishedAt=new Date().toISOString()}={}) {
    if(typeof jobId!=='string'||!jobId||jobId.length>256||typeof runId!=='string'||!runId||runId.length>256||!['ok','fail'].includes(status)||
      !Number.isFinite(Date.parse(finishedAt))||(startedAt!==undefined&&!Number.isFinite(Date.parse(startedAt))))throw storageError('JOBLOG_INVALID_DATA','completion');
    const raw=diagnostic(detail);
    const row={id:jobId,runId,last:finishedAt,finishedAt,...(startedAt?{startedAt}:{}),status,detail:raw.slice(0,2048),summary:raw.slice(0,200),detailTruncated:raw.length>2048,scope:this.scope};
    const file=this.scope==='test'?this.testRunsFile:this.runsFile;
    return persistRun(this.historyFile,row,this.historyLimit,()=>this._readRunFile(file),runs=>atomicWrite(file,JSON.stringify(runs,null,1)));
  }

  _alert(jobId, detail) {
    try {
      const id = String(jobId).replace(/[\r\n]/g, ' ');
      const body = diagnostic(detail).slice(0,2048).split('\n').map(line => '    ' + line).join('\n');
      const entry = `\n## ${ts()} [${id}] 运行失败 {scope:${this.scope}}\n${body}\n（完整记录见 jobs.log 或 route-runs.jsonl）\n`;
      appendFileSync(this.alertsFile, entry);
    } catch(error) {throw storageError('JOBLOG_WRITE_FAILED','alerts',error);}
  }
}
