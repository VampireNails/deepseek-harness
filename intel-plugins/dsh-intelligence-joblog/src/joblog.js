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
  constructor(dir = joblogDir()) {
    this.dir = dir;
    this.logFile = join(dir, "jobs.log");
    this.runsFile = join(dir, "job_runs.json");
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

  readRuns() {
    try {
      return existsSync(this.runsFile) ? JSON.parse(readFileSync(this.runsFile, "utf8")) : {};
    } catch {
      return {};
    }
  }

  readAlerts() {
    try {
      return existsSync(this.alertsFile) ? readFileSync(this.alertsFile, "utf8") : "(无告警)";
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
      const runs = this.readRuns();
      runs[jobId] = { last: ts(), status, detail };
      writeFileSync(this.runsFile, JSON.stringify(runs, null, 1));
    } catch {}
  }

  _alert(jobId, detail) {
    try {
      const entry = `\n## ${ts()} [${jobId}] 运行失败\n${detail}\n（完整堆栈见 jobs.log）\n`;
      appendFileSync(this.alertsFile, entry);
    } catch {}
  }
}
