# dsh-intelligence-approvals —— 审批四级分级

dsh 服务端的审批分级优化（树外插件）。背景：HMC 侧新会话默认
`workspace-write + ask` 已经能弹审批卡；本插件把"一刀切弹卡"细化为四级，
减少只读操作的打扰，同时对高危命令硬拦截。

## 四级

| 级别 | 含义 | pre-execute 处置 |
|------|------|------------------|
| READ | 只读工具（read/grep/glob/web_fetch 等） | `next()` 直接放行，免审批 |
| WRITE | 写工具（write/edit/memory_write/feed_post 等） | `{ kind: "ask" }`，走审批卡 |
| EXEC | 执行命令（bash/pwsh/terminal_send 等） | 先扫高危模式，再 `{ kind: "ask" }` |
| BLOCKED | 高危命令（删根/格盘/写裸设备/fork 炸弹/关机等） | `{ kind: "deny" }`，硬拦截，不弹卡 |

`ask` 走 dsh 的 `approval` 服务（HMC / Web 弹卡）；无审批通道的 profile
会自动降级为拒绝（fail-closed，dsh 核心行为）。未知工具默认 WRITE
（fail-closed：宁可多弹一次卡）。

## 文件

- `index.js` —— 插件入口：`approval_classify` 工具注册 + `tools/pre-execute`
  门（全部走 `ctx.effect`，卸载自动回卷；`ctx.provide("approvals", …)` 给
  其他插件编程式调用）。
- `src/classify.js` —— 纯函数分类核心（无副作用）：`classify(toolName, args, policy)`
  返回 `{ tool, level, disposition, reason }`，`disposition` 为
  `allow / approve / block`。
- `src/policy.js` —— `buildPolicy(config)` 配置合并层。
- `tests/approvals.test.js` —— `node --test` 单元测试（22 个用例）。

## 配置（Config）

```yaml
levels: { web_fetch: BLOCKED }  # 覆盖默认分类表（非法值丢弃）
blocked: [\bevilcmd\b]      # 追加高危正则（非法正则编译时丢弃）
sensitive: [[\bdangerop\b, 危险操作]]  # 追加敏感模式（EXEC+具体理由）
defaultLevel: WRITE             # 未知工具默认级别
monitor: false                  # true 时只记录不拦截（灰度/审计模式；BLOCKED 依然硬拦截）
```

## 挂载

与其他 intel 插件一致：`dsh.bundle.patch` 指向 `cordis.patch.yml`，
由 profile 的 bundle 机制挂载。**绝不修改 dsh 核心 `packages/`。**

## 与 Python 版的差异

- 四级命名：Python 版为 READ/WRITE/APPROVAL/BLOCKED；dsh 版把"需审批"
  拆成 WRITE（写）与 EXEC（执行），`disposition` 统一为 approve，
  方便智能体区分"写文件"与"跑命令"的风险。
- 高危正则收紧：`rm -rf /`、`rm -rf /*`、`chmod 777 /` 要求目标路径后
  跟空白或结尾。Python 版的 `/\s*` 会把 `rm -r /tmp/old` 误判为删根；
  dsh 版中这类正常递归删除降为敏感模式走审批。
- 审批通道：Python 版自建审批卡存储（memory/approvals/）；dsh 版复用
  dsh 原生 `approval` 服务（HMC / Web 卡片），本插件只做分级决策。

## 测试

```sh
npm install
node --test "tests/*.test.js"   # 22/22 通过
```

pre-execute 挂法另经真实 cordis `Context` + `ctx.waterfall("tools/pre-execute", …)`
冒烟验证：read→allow、write→ask、bash `rm -rf /`→deny（APPROVAL_BLOCKED）。
