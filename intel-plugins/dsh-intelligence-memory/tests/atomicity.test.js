import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openStore } from "../src/store.js";
import { FtsRetriever } from "../src/retriever.js";

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "memory-atomicity-"));
  const db = openStore(directory);
  const retriever = new FtsRetriever(db);
  return { db, retriever, close() {
    retriever.close();
    rmSync(directory, { recursive: true, force: true });
  } };
}

function conflictingFtsRow(db) {
  // A real FTS5 row conflicts with the next memories AUTOINCREMENT ID.
  // SQLite forbids triggers on FTS virtual/shadow tables, so use its own constraint.
  db.prepare("INSERT INTO memories_fts(rowid, text) VALUES (?, ?)").run(1, "preexisting orphan fixture");
}

function isSqliteConstraint(error) {
  return error.code === "ERR_SQLITE_ERROR" && error.errcode === 19;
}

test("a rejected FTS insert leaves no memory row and later add/get/search still work", () => {
  const { db, retriever, close } = fixture();
  try {
    conflictingFtsRow(db);
    assert.throws(() => retriever.add({ text: "atomicity rejected memory", sessionId: "anonymous-rejected" }), isSqliteConstraint);
    const residual = db.prepare("SELECT text FROM memories").all().map(row => row.text);
    assert.deepEqual(db.prepare("SELECT rowid, text FROM memories_fts").all().map(row => ({ ...row })),
      [{ rowid: 1, text: "preexisting orphan fixture" }]);
    db.prepare("DELETE FROM memories_fts WHERE rowid = ?").run(1);
    const { id } = retriever.add({ text: "atomicity recovered memory", kind: "note", sessionId: "anonymous-recovered" });
    assert.equal(retriever.get(id).text, "atomicity recovered memory");
    assert.deepEqual(retriever.search("recovered").map(row => row.id), [id]);
    assert.deepEqual(residual, [], "The first INSERT must roll back when the FTS INSERT fails");
    assert.equal(db.prepare("SELECT count(*) AS count FROM memories").get().count, 1);
  } finally {
    close();
  }
});

test("a failed add inside caller BEGIN preserves caller work and allows a later commit", () => {
  const { db, retriever, close } = fixture();
  try {
    db.exec("CREATE TABLE caller_work (value TEXT NOT NULL)");
    conflictingFtsRow(db);
    db.exec("BEGIN");
    db.prepare("INSERT INTO caller_work(value) VALUES (?)").run("before rejected add");
    assert.throws(() => retriever.add({ text: "atomicity rejected nested memory" }), isSqliteConstraint);
    const residual = db.prepare("SELECT text FROM memories").all().map(row => row.text);
    assert.equal(db.prepare("SELECT text FROM memories_fts WHERE rowid = 1").get().text, "preexisting orphan fixture");
    assert.deepEqual(db.prepare("SELECT value FROM caller_work").all().map(row => row.value), ["before rejected add"]);
    db.prepare("INSERT INTO caller_work(value) VALUES (?)").run("after rejected add");
    db.prepare("DELETE FROM memories_fts WHERE rowid = ?").run(1);
    const { id } = retriever.add({ text: "atomicity nested recovery" });
    assert.deepEqual(retriever.search("recovery").map(row => row.id), [id]);
    assert.doesNotThrow(() => db.exec("COMMIT"), "add must leave the caller's transaction active");
    assert.deepEqual(db.prepare("SELECT value FROM caller_work").all().map(row => row.value),
      ["before rejected add", "after rejected add"]);
    assert.deepEqual(residual, [], "Failed add must undo its own row without rolling back caller work");
    assert.equal(db.prepare("SELECT count(*) AS count FROM memories").get().count, 1);
  } finally {
    close();
  }
});

test("successful add remains part of caller BEGIN and caller ROLLBACK removes both records", () => {
  const { db, retriever, close } = fixture();
  try {
    db.exec("CREATE TABLE caller_work (value TEXT NOT NULL); BEGIN");
    db.prepare("INSERT INTO caller_work(value) VALUES (?)").run("caller-owned rollback");
    const { id } = retriever.add({ text: "atomicity rollback marker" });
    assert.equal(retriever.get(id).text, "atomicity rollback marker");
    assert.deepEqual(retriever.search("rollback").map(row => row.id), [id]);
    assert.doesNotThrow(() => db.exec("ROLLBACK"), "add must not commit the caller's transaction");
    assert.equal(retriever.get(id), null);
    assert.deepEqual(retriever.search("rollback"), []);
    for (const table of ["memories", "memories_fts", "caller_work"])
      assert.equal(db.prepare(`SELECT count(*) AS count FROM ${table}`).get().count, 0);
    const recovered = retriever.add({ text: "atomicity recovery after rollback" });
    assert.deepEqual(retriever.search("recovery").map(row => row.id), [recovered.id]);
  } finally {
    close();
  }
});

test("orphan FTS hits cannot consume the result limit before the memories join", () => {
  const { db, retriever, close } = fixture();
  try {
    const { id } = retriever.add({ text: "orphanbudget legitimate memory with extra surrounding words" });
    const insert = db.prepare("INSERT INTO memories_fts(rowid, text) VALUES (?, ?)");
    for (let index = 0; index < 40; index++) insert.run(1000 + index, "orphanbudget");
    assert.deepEqual(retriever.search("orphanbudget", 1).map(row => row.id), [id]);
  } finally {
    close();
  }
});
