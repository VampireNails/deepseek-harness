/**
 * build-lib-host.mjs — 分批构建 host 产物（替代一次性 `tsdown` 全并发）
 *
 * 背景：`tsdown` 的 workspace 模式会用 `Promise.all` 一次性并发构建全部 41 个包。
 * 在 Windows + 实时杀毒环境下，并发写 `lib/*.js` 时会被杀毒的写后扫描（scan-on-close）
 * 随机打断，报 `Failed to write file in ... (os error 5) 拒绝访问`，整批构建失败——
 * 这直接导致 pre-push 的 typecheck 挂掉、推送受阻。
 *
 * 判定依据：单包连续构建 8/8 全成功，全并发 4 次挂 3 次 ⇒ 跨包并发争用，非包内竞态。
 *
 * 做法：把包列表切成小批（默认 6 个/批）串行构建，每批失败后重试（默认 3 次）。
 * 分批只是降低瞬时写盘密度，构建配置与产物与一次性构建完全一致。
 *
 * 可调：`DSH_BUILD_BATCH_SIZE`（默认 6）、`DSH_BUILD_BATCH_RETRIES`（默认 3）。
 * 若 `DSH_BUILD_BATCH_SIZE=0` 则退化为一次性全并发（等同旧行为）。
 */
import { spawnSync } from 'node:child_process'
import { expandHostWorkspace } from './tsdown-workspace.mjs'

const PATTERNS = ['vendor/*', 'packages/*/*', 'apps/cli']
const BATCH_SIZE = Number(process.env.DSH_BUILD_BATCH_SIZE ?? 15)
const RETRIES = Number(process.env.DSH_BUILD_BATCH_RETRIES ?? 3)

const all = expandHostWorkspace(PATTERNS)
if (all.length === 0) {
  console.error('[build-lib-host] 未解析到任何 workspace 包，检查 PATTERNS 与 cwd')
  process.exit(1)
}

const batches =
  BATCH_SIZE > 0
    ? Array.from({ length: Math.ceil(all.length / BATCH_SIZE) }, (_, i) => all.slice(i * BATCH_SIZE, (i + 1) * BATCH_SIZE))
    : [all]

console.log(`[build-lib-host] 共 ${all.length} 个包，分 ${batches.length} 批（${BATCH_SIZE > 0 ? BATCH_SIZE : '全部'}/批）`)

for (const [index, batch] of batches.entries()) {
  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    const result = spawnSync(
      process.platform === 'win32' ? 'npx.cmd' : 'npx',
      ['tsdown', '-c', 'tsdown.config.ts', '--env.DSH_BUILD_FACE', 'host'],
      {
        stdio: 'inherit',
        shell: process.platform === 'win32',
        env: { ...process.env, DSH_BUILD_WORKSPACE: batch.join(',') },
      },
    )
    const code = result.status ?? 1
    if (code === 0) break
    if (attempt === RETRIES) {
      console.error(`[build-lib-host] 第 ${index + 1}/${batches.length} 批重试 ${RETRIES} 次仍失败（退出码 ${code}）`)
      process.exit(code)
    }
    console.error(`[build-lib-host] 第 ${index + 1}/${batches.length} 批失败（退出码 ${code}），${attempt}/${RETRIES} 重试…`)
    spawnSync(process.execPath, ['-e', 'setTimeout(() => {}, 2000)'])
  }
  console.log(`[build-lib-host] 第 ${index + 1}/${batches.length} 批完成`)
}

console.log('[build-lib-host] 全部批次构建完成')
