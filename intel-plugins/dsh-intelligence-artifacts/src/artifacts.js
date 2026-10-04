// dsh-intelligence-artifacts —— Artifacts v2 存储层（复刻 Python 版 agent/artifacts.py）。
//
// 数据目录：$DSH_HOME/intel-artifacts（默认 ~/.dsh/intel-artifacts），与生产/测试隔离。
// 目录结构：<dir>/<aid>/meta.json + v1.md / v2.html ……；旧格式（meta 无 versions + artifact.ext）自动兼容。

import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { homedir } from "node:os";
import { randomBytes } from "node:crypto";
import { withStoreWriter, atomicJson, publicationMetadata } from '../../shared/persistence.js';

export const KINDS = { markdown: ".md", html: ".html", text: ".txt" };

export function artifactsDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-artifacts");
}

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function slug(title) {
  const s = String(title || "")
    .trim()
    .replace(/[^\w\u4e00-\u9fff-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${s.slice(0, 30) || "artifact"}-${randomBytes(3).toString("hex")}`;
}

function normalizeKind(kind) {
  const k = String(kind || "markdown").toLowerCase();
  return KINDS[k] ? k : "markdown";
}

export class ArtifactStore {
  constructor(dir = artifactsDir()) {
    this.dir = resolve(dir);
    mkdirSync(this.dir, { recursive: true });
  }

  _safeDir(aid) {
    const d = resolve(join(this.dir, aid || ""));
    if (d !== this.dir && !d.startsWith(this.dir + sep)) return null;
    if (!existsSync(d) || !statSync(d).isDirectory()) return null;
    return d;
  }

  _loadMeta(d) {
    const f = join(d, "meta.json");
    if (!existsSync(f)) return null;
    try {
      return JSON.parse(readFileSync(f, "utf8"));
    } catch {
      return null;
    }
  }

  _saveMeta(d, meta) {
    atomicJson(join(d, "meta.json"), meta);
  }

  // 同 title 再次 save 时复用旧 id（实现版本追加）。
  _findByTitle(title) {
    if (!existsSync(this.dir) || !title) return null;
    for (const name of readdirSync(this.dir)) {
      const d = join(this.dir, name);
      if (!statSync(d).isDirectory()) continue;
      const m = this._loadMeta(d);
      if (m && m.title === title) return m.id;
    }
    return null;
  }

  // 解析版本号到磁盘文件；兼容旧格式（无 versions 时读 artifact.ext）。
  _versionFile(d, meta, v, ext) {
    for (const ver of meta.versions || []) {
      if (ver.v === v) {
        const f = join(d, ver.file);
        if (existsSync(f)) return f;
        return null;
      }
    }
    if (v === 1) {
      const f = join(d, "artifact" + ext);
      if (existsSync(f)) return f;
    }
    return null;
  }

  // 保存产物；同 title 再次保存则复用旧 id、追加为新版本。
  // 返回 {id, title, kind, version, url, path, ...}。
  save(title, content, kind = "markdown", metadata) {
    const identity = publicationMetadata(metadata);
    return withStoreWriter(this.dir, () => {
    kind = normalizeKind(kind);
    if (identity.idempotencyKey) {
      for (const name of readdirSync(this.dir)) {
        const candidate = this._safeDir(name);
        if (!candidate) continue;
        const saved = this._loadMeta(candidate);
        const previous = saved?.versions?.find(version => version.idempotencyKey === identity.idempotencyKey);
        if (!previous) continue;
        const existing = this.get(saved.id, previous.v);
        if (!existing || existing.title !== title || existing.content !== (content || '') || existing.kind !== kind ||
            ['taskId','runId','runDate','memoryDirectory'].some(field => existing[field] !== identity[field]) ||
            JSON.stringify(existing.references ?? []) !== JSON.stringify(identity.references ?? [])) throw Error('publication idempotency conflict');
        return existing;
      }
    }
    const ext = KINDS[kind];
    const aid = this._findByTitle(title) || slug(title || "artifact");
    const d = join(this.dir, aid);
    mkdirSync(d, { recursive: true });

    const meta = this._loadMeta(d) || {};
    let versions = meta.versions || [];
    if (!versions.length) {
      // 旧格式迁移：已有 artifact.ext 算作 v1
      const old = join(d, "artifact" + ext);
      if (existsSync(old)) versions = [{ v: 1, ts: meta.created_at || "", file: "artifact" + ext }];
    }
    const v = versions.length ? versions[versions.length - 1].v + 1 : 1;
    const fname = `v${v}${ext}`;
    writeFileSync(join(d, fname), content || "", "utf8");
    versions.push({ v, ts: ts(), file: fname, kind, ...identity });

    Object.assign(meta, {
      id: aid,
      title: title || aid,
      kind,
      created_at: meta.created_at || ts(),
      updated_at: ts(),
      ts: Date.now() / 1000,
      versions,
      version: v,
      url: `/artifacts/${aid}`,
      path: aid,
    });
    this._saveMeta(d, meta);
    return { ...meta, ...identity };
    });
  }

  // 取产物；v 指定版本号（默认最新）。返回 meta + content + version。
  get(aid, v = null) {
    const d = this._safeDir(aid);
    if (!d) return null;
    const m = this._loadMeta(d);
    if (!m) return null;
    const ext = KINDS[m.kind] || ".md";
    const versions = m.versions || [];
    if (v == null) v = versions.length ? versions[versions.length - 1].v : 1;
    const f = this._versionFile(d, m, v, ext);
    if (!f) return null;
    const entry = versions.find(version => version.v === v);
    const versionIdentity = entry?.idempotencyKey ? publicationMetadata(entry) : entry?.references ? publicationMetadata({ references: entry.references, memoryDirectory: entry.memoryDirectory }) : {};
    return { ...m, ...versionIdentity, kind: entry?.kind ?? m.kind, content: readFileSync(f, "utf8"), version: v, url: `/artifacts/${aid}` };
  }

  // 最新版内容的便捷读取入口（= get(aid)）。
  read(aid) {
    return this.get(aid);
  }

  // 返回版本列表文本。
  versionsText(aid) {
    const d = this._safeDir(aid);
    if (!d) return "未找到该产物";
    const m = this._loadMeta(d);
    if (!m) return "未找到该产物";
    const vs = m.versions || [];
    if (!vs.length) return `产物「${m.title}」暂无版本记录（旧格式单文件）`;
    const lines = [`产物「${m.title}」共 ${vs.length} 个版本：`];
    for (const ver of vs) lines.push(`- v${ver.v}  ${ver.ts}  ${ver.file}`);
    return lines.join("\n");
  }
}
