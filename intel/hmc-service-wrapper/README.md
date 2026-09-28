# hmc-service-wrapper

把 HMC 仓库的 service 插件套成 npm 包，**不修改 HMC 仓库代码**（只读约束）。

- `index.mjs` re-export `/root/intel/hmc-client/service/plugin.mjs`（HMC 只读 clone 位置；如移动需同步修改）
- `cordis.patch.yml` 把本包声明为 dsh bundle
- 被 `intel/bootstrap.sh` 以 `link:` 方式装入 `hmc-test` profile

入库原因（2026-09-28）：旧 `bootstrap.sh` 引用的 `intel/hmc-service-wrapper`
在仓库中不存在，导致 hmc-test profile 无法从零重建。现将实际在用的 wrapper
（原 `/root/intel/hmc-test/wrapper`，与线上 `node_modules` 内的一致）入库版本管理。

## 等价性边界（2026-09-29 第三轮审计 §3.3）

`hmc-test` 是 **API 级冒烟验证 profile**，不等价于手机真机验收环境：

- wrapper 只做 `insert: hmc-service-wrapper`（re-export HMC 插件），未并入 HMC
  仓库 `service/cordis.patch.yml` 的 4 项（禁用 `directory-picker`、重配
  `web-runtime`、插入 `hmc-directory-picker-host/-ui`、配置 `pwsh-sandbox`）。
  其中 `pwsh-sandbox` 依赖 `HMC_PWSH_PATH`（Windows PowerShell 路径），在 Linux
  服务器上无意义——硬并 patch 只会引入平台无关的配置噪音，故意不并。
- 因此 bootstrap 的"三 profile 全过"应理解为"能装、能起、API 通"，不等价于
  "与真机验收环境一致"。

前置条件：`/root/intel/hmc-client`（HMC 只读 clone）必须存在。`index.mjs`
用硬编码绝对路径 re-export，如 clone 位置变化需同步修改——"从零重建"仍依赖
这个仓库外路径。
