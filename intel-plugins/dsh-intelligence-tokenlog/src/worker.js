import { parentPort, workerData } from "node:worker_threads";
import { aggregateSessions } from "./aggregator.js";

parentPort.postMessage(aggregateSessions(workerData.sessionsRoot, workerData.days, undefined,
  { deadline: workerData.deadline, decompressTimeoutMs: workerData.decompressTimeoutMs }));
