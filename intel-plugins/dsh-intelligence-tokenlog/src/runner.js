import { Worker } from "node:worker_threads";

/** Scan outside the Host event loop; cancellation and timeout await worker exit. */
export async function aggregateInWorker(days,
  { sessionsRoot, signal, timeoutMs = 60000, decompressTimeoutMs = 5000, automationIndex } = {}) {
  if (signal?.aborted) throw new Error("TOKENLOG_CANCELLED");
  const worker = new Worker(new URL("./worker.js", import.meta.url), {
    workerData: { days, sessionsRoot, deadline: Date.now() + timeoutMs, decompressTimeoutMs, automationIndex },
  });
  return new Promise((resolve, reject) => {
    let result;
    let failure;
    const stop = code => {
      failure ??= code;
      void worker.terminate();
    };
    const abort = () => stop("TOKENLOG_CANCELLED");
    const timer = setTimeout(() => stop("TOKENLOG_TIMEOUT"), timeoutMs);
    signal?.addEventListener("abort", abort, { once: true });
    worker.once("message", value => { result = value; });
    worker.once("error", error => {
      failure ??= ["TOKENLOG_TIMEOUT", "TOKENLOG_SOURCE_UNAVAILABLE", "TOKENLOG_ZSTD_UNAVAILABLE", "TOKENLOG_ZSTD_NOT_EXECUTABLE", "TOKENLOG_DECOMPRESS_FAILED", "TOKENLOG_LOG_INVALID", "TOKENLOG_LOG_INVALID_UTF8", "TOKENLOG_LOG_UNREADABLE", "TOKENLOG_LOG_MISSING", "TOKENLOG_DUPLICATE_CONFLICT", "TOKENLOG_METADATA_UNREADABLE", "TOKENLOG_METADATA_INVALID"].includes(error.message)
        ? error.message : "TOKENLOG_READ_FAILED";
    });
    worker.once("exit", code => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      if (failure || code !== 0 || !result) reject(new Error(failure || "TOKENLOG_READ_FAILED"));
      else resolve(result);
    });
  });
}
