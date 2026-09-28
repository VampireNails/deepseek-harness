# hmc-service-wrapper

把 HMC 仓库的 service 插件套成 npm 包，**不修改 HMC 仓库代码**（只读约束）。

- `index.mjs` re-export `/root/intel/hmc-client/service/plugin.mjs`（HMC 只读 clone 位置；如移动需同步修改）
- `cordis.patch.yml` 把本包声明为 dsh bundle
- 被 `intel/bootstrap.sh` 以 `link:` 方式装入 `hmc-test` profile

入库原因（2026-09-28）：旧 `bootstrap.sh` 引用的 `intel/hmc-service-wrapper`
在仓库中不存在，导致 hmc-test profile 无法从零重建。现将实际在用的 wrapper
（原 `/root/intel/hmc-test/wrapper`，与线上 `node_modules` 内的一致）入库版本管理。
