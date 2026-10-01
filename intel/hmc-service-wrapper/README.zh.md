# hmc-service-wrapper

[English](README.md) | 中文

本包重新导出 /root/intel/hmc-client/service/plugin.mjs，不修改 HMC 源码。cordis.patch.yml 将 wrapper 注册为 DSH bundle；intel/bootstrap.sh 通过 file: 依赖，由 npm 复制到 hmc-test profile。HMC checkout 必须存在于声明的绝对路径。

## 验证范围

hmc-test 提供 API 冒烟组合，不包含 HMC 完整的 service/cordis.patch.yml 中的目录选择器替换、web-runtime 配置和 PowerShell 沙箱配置。HMC_PWSH_PATH 尤其属于 Windows 安装环境。bootstrap 成功仅证明 profile 安装完成；Host 启动和 API 可用性需要单独冒烟验证。手机行为需要实际配对 Host 和设备验收。
