import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { openStore } from "../src/store.js";
import { FtsRetriever } from "../src/retriever.js";

const damagedCounts = { memoryRows: 3, ftsRows: 3, orphanFtsRows: 1, missingFtsRows: 1, mismatchedFtsRows: 1 };
const healthyCounts = { memoryRows: 3, ftsRows: 3, orphanFtsRows: 0, missingFtsRows: 0, mismatchedFtsRows: 0 };
const maintenanceScript = fileURLToPath(new URL("../src/maintenance.js", import.meta.url));
const sha256 = value => createHash("sha256").update(value).digest("hex");

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "memory-maintenance-"));
  const databasePath = join(directory, "memory.db");
  const backupPath = join(directory, "original-index.backup.db");
  const db = openStore(directory);
  try {
    const insert = db.prepare("INSERT INTO memories(id, text, kind, created_at, session_id) VALUES (?, ?, ?, ?, ?)");
    insert.run(7, "红色自行车需要打气", "note", 1791129600000, "anonymous-cjk");
    insert.run(11, "English maintenance marker", "decision", 1791129601000, null);
    insert.run(29, "恢复索引以后能够检索", "note", 1791129602000, "anonymous-wrong-index");
    const fts = db.prepare("INSERT INTO memories_fts(rowid, text) VALUES (?, ?)");
    fts.run(7, "红 色 自 行 车 需 要 打 气");
    fts.run(29, "unrelated stale indexed text");
    fts.run(900, "orphan index marker");
  } finally {
    db.close();
  }
  return { directory, databasePath, backupPath, cleanup() { rmSync(directory, { recursive: true, force: true }); } };
}

function withDatabase(path, callback, readOnly = true) {
  const db = new DatabaseSync(path, { readOnly });
  try { return callback(db); }
  finally { db.close(); }
}

function snapshot(path) {
  return withDatabase(path, db => ({
    memories: db.prepare("SELECT id, text, kind, created_at, session_id FROM memories ORDER BY id, text").all().map(row => ({ ...row })),
    fts: db.prepare("SELECT rowid, text FROM memories_fts ORDER BY rowid").all().map(row => ({ ...row })),
  }));
}

test("inspect reports index counts without exposing memory text or changing SQLite", async () => {
  const { inspectMemoryIndex } = await import("../src/maintenance.js");
  const f = fixture();
  try {
    const originalBytes = readFileSync(f.databasePath);
    const report = withDatabase(f.databasePath, db => inspectMemoryIndex(db));
    assert.deepEqual(report, damagedCounts);
    assert.deepEqual(readFileSync(f.databasePath), originalBytes);
  } finally { f.cleanup(); }
});

test("repair backs up the damaged SQLite, preserves every memory field and restores CJK and English search", async () => {
  const { inspectMemoryIndex, repairMemoryIndex } = await import("../src/maintenance.js");
  const f = fixture();
  try {
    const original = snapshot(f.databasePath);
    const repaired = await repairMemoryIndex({ databasePath: f.databasePath, backupPath: f.backupPath });
    assert.deepEqual(repaired.before, damagedCounts);
    assert.deepEqual(repaired.after, healthyCounts);
    assert.equal(repaired.backupPathSha256, sha256(f.backupPath));
    assert.equal(repaired.backupSha256, sha256(readFileSync(f.backupPath)));
    assert.equal(Object.hasOwn(repaired, "backup"), false);
    assert.match(repaired.memoryRowsSha256, /^[a-f0-9]{64}$/);
    assert.deepEqual(snapshot(f.databasePath).memories, original.memories);
    assert.deepEqual(snapshot(f.backupPath), original, "Backup must retain the original bad FTS and all memory fields");
    assert.deepEqual(withDatabase(f.backupPath, db => inspectMemoryIndex(db)), damagedCounts);
    assert.deepEqual(withDatabase(f.databasePath, db => inspectMemoryIndex(db)), healthyCounts);

    const once = snapshot(f.databasePath);
    const secondBackup = join(f.directory, "healthy-index.backup.db");
    const repeated = await repairMemoryIndex({ databasePath: f.databasePath, backupPath: secondBackup });
    assert.deepEqual(repeated.before, healthyCounts);
    assert.deepEqual(repeated.after, healthyCounts);
    assert.equal(repeated.memoryRowsSha256, repaired.memoryRowsSha256);
    assert.deepEqual(snapshot(f.databasePath), once, "A second repair must not change logical memory or index rows");
    assert.deepEqual(snapshot(secondBackup), once);

    withDatabase(f.databasePath, db => {
      const retriever = new FtsRetriever(db);
      assert.deepEqual(retriever.search("红色自行车").map(row => row.id), [7]);
      assert.deepEqual(retriever.search("English").map(row => row.id), [11]);
      assert.deepEqual(retriever.search("恢复索引").map(row => row.id), [29]);
      assert.deepEqual(retriever.search("orphan"), []);
      const { id } = retriever.add({ text: "maintenance subsequent memory", sessionId: "anonymous-after-repair" });
      assert.equal(retriever.get(id).text, "maintenance subsequent memory");
      assert.deepEqual(retriever.search("subsequent").map(row => row.id), [id]);
      assert.deepEqual(inspectMemoryIndex(db), { ...healthyCounts, memoryRows: 4, ftsRows: 4 });
    }, false);
  } finally { f.cleanup(); }
});

test("repair rejects relative paths, missing databases and existing backups without creating or changing files", async () => {
  const { repairMemoryIndex } = await import("../src/maintenance.js");
  const f = fixture();
  try {
    const originalBytes = readFileSync(f.databasePath);
    const uncreatedBackup = join(f.directory, "must-not-exist.db");
    await assert.rejects(() => repairMemoryIndex({ databasePath: relative(f.directory, f.databasePath), backupPath: uncreatedBackup }));
    assert.equal(existsSync(uncreatedBackup), false);
    await assert.rejects(() => repairMemoryIndex({ databasePath: f.databasePath, backupPath: relative(f.directory, uncreatedBackup) }));
    assert.equal(existsSync(uncreatedBackup), false);
    const missingParent = join(f.directory, "missing-parent");
    const missingDatabase = join(missingParent, "memory.db");
    await assert.rejects(() => repairMemoryIndex({ databasePath: missingDatabase, backupPath: uncreatedBackup }));
    assert.equal(existsSync(missingDatabase), false);
    assert.equal(existsSync(missingParent), false);
    assert.equal(existsSync(uncreatedBackup), false);
    const existingBackup = join(f.directory, "existing-backup.db");
    const sentinel = Buffer.from("own fixture existing backup must be preserved");
    writeFileSync(existingBackup, sentinel);
    await assert.rejects(() => repairMemoryIndex({ databasePath: f.databasePath, backupPath: existingBackup }));
    assert.deepEqual(readFileSync(existingBackup), sentinel);
    await assert.rejects(() => repairMemoryIndex({ databasePath: f.databasePath, backupPath: f.databasePath }));
    assert.deepEqual(readFileSync(f.databasePath), originalBytes);
  } finally { f.cleanup(); }
});

test("a real duplicate-rowid constraint during repair rolls back FTS changes and retains the original backup", async () => {
  const { repairMemoryIndex } = await import("../src/maintenance.js");
  const f = fixture();
  try {
    // Own damaged fixture only: duplicate IDs force FTS5's second INSERT to fail.
    // No trigger, mock database, production schema or implementation hook is involved.
    withDatabase(f.databasePath, db => {
      db.exec(`
        ALTER TABLE memories RENAME TO fixture_original_memories;
        CREATE TABLE memories (id INTEGER NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL, created_at INTEGER NOT NULL, session_id TEXT);
        INSERT INTO memories SELECT id, text, kind, created_at, session_id FROM fixture_original_memories;
        INSERT INTO memories VALUES (7, 'duplicate ID fixture memory', 'note', 1791129603000, 'anonymous-duplicate');
        DROP TABLE fixture_original_memories;
      `);
    }, false);
    const original = snapshot(f.databasePath);
    const constraint = error => error?.errcode === 19 || /constraint|unique/i.test(error?.message ?? "") ||
      (error?.cause && constraint(error.cause)) || (error instanceof AggregateError && error.errors.some(constraint));
    await assert.rejects(() => repairMemoryIndex({ databasePath: f.databasePath, backupPath: f.backupPath }), constraint);
    assert.deepEqual(snapshot(f.databasePath), original, "A failed repair must roll back earlier FTS deletion/insertion");
    assert.equal(existsSync(f.backupPath), true, "The original SQLite backup must already exist before mutation");
    assert.deepEqual(snapshot(f.backupPath), original);
  } finally { f.cleanup(); }
});

function runCli(argv, cwd) {
  const env = {};
  for (const key of ["PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"])
    if (process.env[key] !== undefined) env[key] = process.env[key];
  const result = spawnSync(process.execPath, [maintenanceScript, ...argv], {
    cwd, env, encoding: "utf8", timeout: 10000, maxBuffer: 1024 * 1024,
  });
  assert.equal(result.error, undefined, "The real maintenance CLI must start and finish within the fixture deadline");
  assert.equal(result.signal, null);
  return result;
}

test("the real inspect CLI emits only counts and leaves the existing database unchanged", () => {
  const f = fixture();
  try {
    const originalBytes = readFileSync(f.databasePath);
    const original = snapshot(f.databasePath);
    const result = runCli(["inspect", f.databasePath], f.directory);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), damagedCounts);
    for (const row of original.memories) {
      assert.equal(result.stdout.includes(row.text), false);
      if (row.session_id !== null) assert.equal(result.stdout.includes(row.session_id), false);
    }
    assert.equal(result.stdout.includes(f.databasePath), false);
    assert.deepEqual(readFileSync(f.databasePath), originalBytes);
    assert.equal(existsSync(f.backupPath), false);
  } finally { f.cleanup(); }
});

test("the real repair CLI publishes hashes and counts without private backup paths or session markers", () => {
  const f = fixture();
  try {
    const privateMarker = "private-anonymous-session-marker-548";
    const privateText = "private anonymous memory content marker";
    const privateBackup = join(f.directory, privateMarker + ".backup.db");
    withDatabase(f.databasePath, db => {
      db.prepare("UPDATE memories SET text=?, session_id=? WHERE id=11").run(privateText, privateMarker);
    }, false);
    const original = snapshot(f.databasePath);
    const result = runCli(["repair", f.databasePath, privateBackup], f.directory);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.includes(privateMarker), false, "Public stdout must not echo a private marker in the backup filename");
    assert.equal(result.stdout.includes(privateBackup), false, "Public stdout must not disclose the backup path");
    for (const row of original.memories) {
      assert.equal(result.stdout.includes(row.text), false, "Public stdout must not disclose memory text");
      if (row.session_id !== null) assert.equal(result.stdout.includes(row.session_id), false, "Public stdout must not disclose session IDs");
    }
    const report = JSON.parse(result.stdout);
    assert.deepEqual(Object.keys(report).sort(), ["after", "backupPathSha256", "backupSha256", "before", "memoryRowsSha256"].sort());
    assert.deepEqual(report.before, damagedCounts);
    assert.deepEqual(report.after, healthyCounts);
    assert.equal(report.backupPathSha256, sha256(privateBackup));
    assert.equal(report.backupSha256, sha256(readFileSync(privateBackup)));
    assert.match(report.memoryRowsSha256, /^[a-f0-9]{64}$/);
    assert.deepEqual(snapshot(privateBackup), original);
    assert.deepEqual(snapshot(f.databasePath).memories, original.memories);
  } finally { f.cleanup(); }
});

test("the real CLI rejects invalid mode and missing absolute arguments without creating or modifying SQLite", () => {
  const f = fixture();
  try {
    const originalBytes = readFileSync(f.databasePath);
    const missingDatabase = join(f.directory, "missing.db");
    const invalidArguments = [
      [], ["unknown", f.databasePath], ["inspect"], ["inspect", "memory.db"],
      ["repair", f.databasePath], ["repair", f.databasePath, "relative-backup.db"],
      ["inspect", f.databasePath, f.backupPath], ["repair", f.databasePath, f.backupPath, "extra"],
      ["inspect", missingDatabase], ["repair", missingDatabase, f.backupPath],
    ];
    for (const argv of invalidArguments) {
      const result = runCli(argv, f.directory);
      assert.notEqual(result.status, 0, "Invalid arguments must exit unsuccessfully: " + JSON.stringify(argv));
      assert.equal(result.stdout.trim(), "", "Rejected calls must not print a success report");
      assert.equal(result.stderr.includes(f.databasePath), false);
      assert.deepEqual(readFileSync(f.databasePath), originalBytes);
      assert.equal(existsSync(f.backupPath), false);
      assert.equal(existsSync(missingDatabase), false);
      assert.equal(existsSync(join(f.directory, "relative-backup.db")), false);
    }
    const sentinel = Buffer.from("fixture existing backup is private and must stay intact");
    writeFileSync(f.backupPath, sentinel);
    const duplicateBackup = runCli(["repair", f.databasePath, f.backupPath], f.directory);
    assert.notEqual(duplicateBackup.status, 0);
    assert.equal(duplicateBackup.stdout.trim(), "");
    assert.deepEqual(readFileSync(f.backupPath), sentinel);
    assert.deepEqual(readFileSync(f.databasePath), originalBytes);
  } finally { f.cleanup(); }
});
