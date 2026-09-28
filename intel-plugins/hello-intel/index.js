// hello-intel: 阶段 0 最小插件 —— 只验证"能挂上、能加载、能解析 dsh 依赖"
import { createUserMessage } from "@deepseek-ai/dsh-llm";

export const name = "hello-intel";

export function apply(ctx) {
  // 按任务书硬约束 4: 注册走 ctx.effect, 可卸载
  ctx.effect(() => {
    // 构造一条消息证明导入可用, 不实际注入
    const probe = createUserMessage({ content: [{ type: "text", text: "hello-intel loaded" }] });
    if (!probe) throw new Error("createUserMessage unavailable");
    return () => {};
  });
}
