import { createHash } from "node:crypto";
import { closeSync, existsSync, lstatSync, openSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { DatabaseSync, backup } from "node:sqlite";
import { spaceCjk } from "./retriever.js";

/** Inspect logical index consistency without exposing the underlying memory text. */
export function inspectMemoryIndex(db) {
  const count = sql => db.prepare(sql).get().count;
  let mismatchedFtsRows = 0;
  for (const row of db.prepare("SELECT m.text AS original, f.text AS indexed FROM memories m JOIN memories_fts f ON f.rowid=m.id").iterate())
    if (spaceCjk(row.original) !== row.indexed) mismatchedFtsRows++;
  return {
    memoryRows: count("SELECT count(*) AS count FROM memories"),
    ftsRows: count("SELECT count(*) AS count FROM memories_fts"),
    orphanFtsRows: count("SELECT count(*) AS count FROM memories_fts f LEFT JOIN memories m ON m.id=f.rowid WHERE m.id IS NULL"),
    missingFtsRows: count("SELECT count(*) AS count FROM memories m LEFT JOIN memories_fts f ON f.rowid=m.id WHERE f.rowid IS NULL"),
    mismatchedFtsRows,
  };
}

function memoryHash(db) {
  const digest = createHash("sha256");
  const rows = db.prepare("SELECT * FROM memories ORDER BY id,rowid");
  rows.setReadBigInts(true);
  for (const row of rows.iterate())
    digest.update(JSON.stringify(row, (_, value) => typeof value === "bigint" ? { integer: value.toString() } : value) + "\n");
  return digest.digest("hex");
}

/**
 * Explicit offline maintenance: back up SQLite, then rebuild only the derived FTS rows.
 * Writers should be quiescent for the backup/repair window. Rollback of a running system
 * must rebuild from its current memories, never restore this snapshot over later writes.
 */
export async function repairMemoryIndex({ databasePath, backupPath }) {
  if (typeof databasePath !== "string" || typeof backupPath !== "string" ||
      !isAbsolute(databasePath) || !isAbsolute(backupPath) || resolve(databasePath) === resolve(backupPath) ||
      !existsSync(databasePath) || !lstatSync(databasePath).isFile() || existsSync(backupPath))
    throw new Error("existing database and distinct unused absolute backup path required");
  const source = new DatabaseSync(databasePath, { readOnly: true });
  try {
    inspectMemoryIndex(source);
    // Reserve the destination with private permissions; never overwrite another backup.
    const descriptor = openSync(backupPath, "wx", 0o600);
    closeSync(descriptor);
    await backup(source, backupPath);
  } finally { source.close(); }
  const backupSha256 = createHash("sha256").update(readFileSync(backupPath)).digest("hex");
  const db = new DatabaseSync(databasePath);
  try {
    db.exec("PRAGMA busy_timeout=3000; BEGIN IMMEDIATE");
    try {
      const before = inspectMemoryIndex(db), memoryRowsSha256 = memoryHash(db);
      if (before.orphanFtsRows || before.missingFtsRows || before.mismatchedFtsRows) {
        db.exec("DELETE FROM memories_fts");
        const insert = db.prepare("INSERT INTO memories_fts(rowid,text) VALUES (?,?)");
        for (const row of db.prepare("SELECT id,text FROM memories ORDER BY id,rowid").iterate())
          insert.run(row.id, spaceCjk(row.text));
      }
      const after = inspectMemoryIndex(db);
      if (memoryRowsSha256 !== memoryHash(db) || after.orphanFtsRows || after.missingFtsRows || after.mismatchedFtsRows)
        throw new Error("memory index repair invariant failed");
      db.exec("COMMIT");
      const backupPathSha256 = createHash("sha256").update(backupPath).digest("hex");
      return { before, after, backupPathSha256, backupSha256, memoryRowsSha256 };
    } catch (error) { db.exec("ROLLBACK"); throw error; }
  } finally { db.close(); }
}

/** Operator-only CLI; no implicit production path and no memory text in its output. */
export async function memoryMaintenanceCli(argv) {
  const [mode, databasePath, backupPath, ...extra] = argv;
  if (extra.length || !isAbsolute(databasePath ?? "") || !existsSync(databasePath))
    throw new Error("explicit existing absolute memory database required");
  if (mode === "repair" && backupPath) return repairMemoryIndex({ databasePath, backupPath });
  if (mode !== "inspect" || backupPath) throw new Error("use inspect database or repair database unused-backup");
  const db = new DatabaseSync(databasePath, { readOnly: true });
  try { return inspectMemoryIndex(db); } finally { db.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { console.log(JSON.stringify(await memoryMaintenanceCli(process.argv.slice(2)), null, 2)); }
  catch { console.error("Memory index maintenance failed; inspect the private database and backup state."); process.exitCode = 1; }
}
