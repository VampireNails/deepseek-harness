// PC maintenance only: stop the Host and verify the task/descendants before using this script.
import { parseArgs } from "node:util";
import { HookManager } from "../src/manager.js";
import { HookStore } from "../src/store.js";

const { values } = parseArgs({ options: {
  "data-dir": { type: "string" },
  hook: { type: "string" },
  job: { type: "string" },
  action: { type: "string" },
  "max-pending-bytes": { type: "string" },
  "confirm-host-and-task-stopped": { type: "boolean", default: false },
  "acknowledge-duplicate-risk": { type: "boolean", default: false },
} });

try {
  if (!values["data-dir"] || !values.hook || !values.job || !values.action) {
    throw new Error("需要 --data-dir、--hook、--job、--action retry|release；先备份并确认 Host 和任务已停止");
  }
  const manager = new HookManager({ store: new HookStore(values["data-dir"]),
    dispatch: values["max-pending-bytes"] ? { maxPendingBytes: Number(values["max-pending-bytes"]) } : {} });
  manager.stop();
  const result = manager.recoverDispatch(values.hook, {
    job: values.job,
    action: values.action,
    confirmStopped: values["confirm-host-and-task-stopped"],
    acknowledgeDuplicateRisk: values["acknowledge-duplicate-risk"],
  });
  console.log(JSON.stringify(result));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
