// tools.js —— hook_register / hook_list / hook_remove / hook_fire
import { defineTool} from "@deepseek-ai/dsh-tools";

const textBlock = (text) => [{ type: "text", text}];
const jsonOutput = (render) => ({ schema: { type: "json"}, render});

export function hookRegisterTool(manager) {
return defineTool({
name: "hook_register",
description:
"注册或更新一个 hook（事件自动化）。trigger_type 为 file 时轮询数据目录下的子目录（默认 inbox），" +
"新文件只触发一次；为 webhook 时由外部事件通过 hook_fire(source=webhook) 触发。prompt 模板支持 " +
"{name} / {path} / {payload} 占位。触发后自动调度执行（有界：单 hook 冷却 15 分钟、全局并发 3、" +
"单 hook 串行，多次触发合并为一批经 intel-task.sh 起 headless 任务）；auto=false 则只记录不执行。",
parameters: {
name: { type: "string", required: true, description: "hook 名称，如 inbox、deploy-notify"},
trigger_type: {
type: "string",
required: true,
description: "触发类型：file（轮询目录）或 webhook（外部事件）",
},
prompt: {
type: "string",
required: true,
description: "prompt 模板，支持 {name}/{path}/{payload} 占位",
},
dir: { type: "string", description: "file 触发器的轮询子目录（相对数据目录），默认 inbox"},
desc: { type: "string", description: "hook 描述"},
auto: { type: "boolean", description: "触发后是否自动调度执行，默认 true；false 则只记录不执行"},
},
output: jsonOutput((args, value) => textBlock(value.text)),
execute: async (args) => {
try {
const n = manager.register(args.name, {
trigger: { type: args.trigger_type, dir: args.dir},
prompt: args.prompt,
desc: args.desc,
auto: args.auto,
});
return { ok: true, text: `已注册 hook`};
} catch (err) {
return { ok: false, text: `注册失败：${err?.message?? err}`};
}
},
});
}

export function hookListTool(manager) {
return defineTool({
name: "hook_list",
description: "列出所有 hook（内置 + 自定义）：名称、触发类型、描述。",
parameters: {},
output: jsonOutput((args, value) => textBlock(value.text)),
execute: async () => {
try {
const hooks = manager.list();
const names = Object.keys(hooks);
if (!names.length) return { ok: true, text: "还没有 hook。"};
const lines = [`hook（${names.length} 个）：`];
for (const n of names) {
const h = hooks[n];
const trig =
h.trigger?.type === "file"? `file:${h.trigger.dir || "inbox"}`: h.trigger?.type || "?";
lines.push(`${trig}${h.auto === false ? "[不自动执行]" : ""}${h.desc? " —— " + h.desc: ""}`);
}
return { ok: true, text: lines.join("\n")};
} catch (err) {
return { ok: false, text: `查询失败：${err?.message?? err}`};
}
},
});
}

export function hookRemoveTool(manager) {
return defineTool({
name: "hook_remove",
description: "删除一个自定义 hook。内置 hook（如 inbox）不能删除，只能用 hook_register 覆盖。",
parameters: {
name: { type: "string", required: true, description: "hook 名称"},
},
output: jsonOutput((args, value) => textBlock(value.text)),
execute: async (args) => {
try {
const n = manager.remove(args.name);
return { ok: true, text: `已删除 hook`};
} catch (err) {
return { ok: false, text: `删除失败：${err?.message?? err}`};
}
},
});
}

export function hookFireTool(manager) {
return defineTool({
name: "hook_fire",
description:
"手动触发一次 hook（测试/外部事件用）。渲染 prompt 模板并记入任务队列，返回渲染后的任务文本。",
parameters: {
name: { type: "string", required: true, description: "hook 名称"},
source: {
type: "string",
description: "事件来源：manual（默认，任意触发器）/ webhook（仅 webhook 触发器）",
},
path: { type: "string", description: "文件事件路径（file 触发用）"},
payload: { type: "string", description: "事件载荷（字符串或 JSON 字符串）"},
},
output: jsonOutput((args, value) => textBlock(value.text)),
execute: async (args) => {
try {
const prompt = await manager.fire(args.name, {
source: args.source || "manual",
path: args.path || "",
payload: args.payload?? null,
});
return { ok: true, text: prompt};
} catch (err) {
return { ok: false, text: `触发失败：${err?.message?? err}`};
}
},
});
}
