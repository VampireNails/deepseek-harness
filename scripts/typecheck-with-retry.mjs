/**
 * typecheck-with-retry.mjs — 带重试的类型检查入口
 *
 * 背景（Windows + 实时杀毒的环境问题，非代码问题）：
 *   `build:lib:host` 阶段由 rolldown 并发写 41 个包的 `lib/*.js`；杀毒软件会对刚落盘的
 *   JS 做写后扫描（scan-on-close），在扫描窗口内 rolldown 再写同一文件会得到
 *   `Failed to write file in ... (os error 5) 拒绝访问`，整个构建被中止。
 *   该失败是**瞬时的**：事后立刻独占打开同一文件即可成功；重试构建也基本一次即过。
 *   实测单跑失败率约 25%（4 次里挂 3 次，每次锁的文件都不同），故对构建阶段做重试。
 *
 * 设计约束：
 *   - 只重试**构建**阶段（受瞬时锁影响），类型检查 `tsc` 阶段不重试：真实类型错误应当立刻失败。
 *   - 重试上限与次数可配：`DSH_TYPECHECK_RETRIES`（默认 4）。
 *   - 不吞错误：全部重试失败后按最后一次的退出码退出，输出原样透传。
 */
import { spawnSync } from 'node:child_process'

const MAX_ATTEMPTS = Number(process.env.DSH_TYPECHECK_RETRIES ?? 4)

function run(script, label) {
  const isWindows = process.platform === 'win32'
  // npm 在 Windows 上通过 .cmd 分发，需走 shell 才能解析
  const result = spawnSync('npm', ['run', script], { stdio: 'inherit', shell: isWindows })
  return result.status ?? 1
}

for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
  const code = run('build:lib:host')
  if (code === 0) {
    process.exit(run('typecheck:contracts-ready'))
  }
  const left = MAX_ATTEMPTS - attempt
  console.error(
    `\n[typecheck-with-retry] 构建阶段失败（退出码 ${code}）。` +
      (left > 0
        ? `疑似杀毒软件写后扫描导致的瞬时文件锁，${attempt}/${MAX_ATTEMPTS}，2 秒后重试…`
        : `已重试 ${MAX_ATTEMPTS} 次仍失败，请检查上面输出的真实错误。`),
  )
  if (left > 0) spawnSync(process.execPath, ['-e', 'setTimeout(()=>{}, 2000)'])
}

process.exit(1)
