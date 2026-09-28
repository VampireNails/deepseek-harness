// Wrapper: 不修改 HMC 仓库代码，只做 npm 包封装
// HMC 仓库（只读）位于 /root/intel/hmc-client；本文件假设该路径存在。
// 如 HMC clone 位置变化，同步修改下一行的绝对路径。
export * from "/root/intel/hmc-client/service/plugin.mjs";
