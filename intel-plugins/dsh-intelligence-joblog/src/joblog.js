import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export function joblogDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-joblog");
}

const MAX_LOG_LINES = 2000;

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 后台任务的运行日志与失败告警（复刻 Python 版 joblog 三件套）。
// 不依赖 dsh，可被 cron 包装脚本直接 require。
export class JobLog {
  constructor(dir = joblogDir(), { scope = 'production' } = {}) {
    if (!['production', 'test'].includes(scope)) throw new Error('Invalid JobLog scope');
    this.scope = scope;
    this.dir = dir;
    this.logFile = join(dir, "jobs.log");
    this.runsFile = join(dir, "job_runs.json");
    this.testRunsFile = join(dir, "job_test_runs.json");
    this.alertsFile = join(dir, "job_alerts.md");
    mkdirSync(dir, { recursive: true });
  }

  // 包装一个任务：记日志、更新运行状态、失败告警
  async guarded(jobId, fn) {
    this._log(`[${ts()}] ${jobId} 开始`);
    try {
      const result = await fn();
      const detail = String(result ?? "").slice(0, 200);
      this._recordRun(jobId, "ok", detail);
      this._log(`[${ts()}] ${jobId} 成功 ${detail}`);
      return result;
    } catch (e) {
      const detail = (e?.message || String(e)).slice(0, 200);
      this._recordRun(jobId, "fail", detail);
      this._alert(jobId, detail);
      this._log(`[${ts()}] ${jobId} 失败 ${detail}`);
      throw e;
    }
  }

  /**
   * Read latest states; unknown history stays visible.
   * @param options - includeTests also selects explicitly marked test records.
   * @returns Selected records without changing the stored projection.
   */
  readRuns({ includeTests = false } = {}) {
    const runs = this._readRunFile(this.runsFile);
    if (!includeTests) return Object.fromEntries(Object.entries(runs).filter(([,run]) => run.scope !== 'test'));
    const selected = {};
    for (const [id, run] of Object.entries(runs)) selected[JSON.stringify([run.scope ?? 'unclassified', id])] = { ...run, id };
    for (const [id, run] of Object.entries(this._readRunFile(this.testRunsFile))) {
      if (run.scope !== 'test') throw Object.assign(new Error('JOBLOG_INVALID_DATA'), { code: 'JOBLOG_INVALID_DATA' });
      const key = JSON.stringify(['test', id]);
      if (Object.hasOwn(selected, key)) throw Object.assign(new Error('JOBLOG_SOURCE_COLLISION'), { code: 'JOBLOG_SOURCE_COLLISION' });
      selected[key] = { ...run, id };
    }
    return selected;
  }

  _readRunFile(file) {
    let raw;
    try { raw = readFileSync(file, 'utf8'); }
    catch (error) {
      if (error.code === 'ENOENT') return {};
      throw Object.assign(new Error('JOBLOG_READ_FAILED'), { code: 'JOBLOG_READ_FAILED' });
    }
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
  selectAlerts(text, { includeTests = false } = {}) {
    if (includeTests) return text;
    // Indented details cannot introduce a scoped record header.
    return text.replace(/\n## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[[^\r\n]*\] 运行失败 \{scope:test\}\n(?:    [^\n]*\n)*（完整记录见 jobs.log 或 route-runs.jsonl）\n/g, '');
  }

  /**
   * Read selected failures, retaining unmarked history and original dates.
   * @param options - includeTests selects all stored failures.
   * @returns Selected text, or the empty-alert message.
   */
  readAlerts(options = {}) {
    try {
      const text = existsSync(this.alertsFile) ? readFileSync(this.alertsFile, "utf8") : '';
      return this.selectAlerts(text, options).trim() || "(无告警)";
    } catch {
      return "(无告警)";
    }
  }

  _log(line) {
    try {
      appendFileSync(this.logFile, line + "\n");
      const lines = readFileSync(this.logFile, "utf8").split("\n");
      if (lines.length > MAX_LOG_LINES + 1) {
        writeFileSync(this.logFile, lines.slice(-MAX_LOG_LINES).join("\n"));
      }
    } catch {}
  }

  _recordRun(jobId, status, detail) {
    try {
      const file = this.scope === 'test' ? this.testRunsFile : this.runsFile;
      const runs = this._readRunFile(file);
      runs[jobId] = { last: ts(), status, detail, scope: this.scope };
      writeFileSync(file, JSON.stringify(runs, null, 1), { mode: 0o600 });
    } catch {}
  }

  _alert(jobId, detail) {
    try {
      const id = String(jobId).replace(/[\r\n]/g, ' ');
      const body = String(detail).split('\n').map(line => '    ' + line).join('\n');
      const entry = `\n## ${ts()} [${id}] 运行失败 {scope:${this.scope}}\n${body}\n（完整记录见 jobs.log 或 route-runs.jsonl）\n`;
      appendFileSync(this.alertsFile, entry);
    } catch {}
  }
}
