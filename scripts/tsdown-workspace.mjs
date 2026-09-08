/**
 * tsdown workspace 解析（供 tsdown.config.ts 与 scripts/build-lib-host.mjs 共用）
 *
 * 为什么需要它：
 *   41 个包一次性全并发构建时，Windows 上实时杀毒会对刚落盘的 `lib/*.js` 做写后扫描，
 *   构建进程再写同一文件会随机拿到 `os error 5 拒绝访问` 而整体失败（实测全并发失败率
 *   约 25%~75%，且每次锁的文件都不同；单包构建 8/8 全成功 ⇒ 是跨包并发争用，不是包内竞态）。
 *   因此构建改为分批执行，由 `DSH_BUILD_WORKSPACE` 传入本批要构建的包（逗号分隔）。
 *
 * 若未设置该环境变量，行为与改造前完全一致（原样返回 glob 模式，交给 tsdown 自己解析）。
 */
import fs from 'node:fs'
import path from 'node:path'

/** 展开 glob 模式（支持单级与两级星号通配）为含 package.json 的具体目录 */
export function expandWorkspacePatterns(patterns, root = process.cwd()) {
  const out = new Set()
  for (const pattern of patterns) {
    const segments = pattern.split('/')
    let dirs = [root]
    for (const segment of segments) {
      if (segment === '*') {
        const next = []
        for (const dir of dirs) {
          if (!fs.existsSync(dir)) continue
          for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            if (entry.isDirectory()) next.push(path.join(dir, entry.name))
          }
        }
        dirs = next
      } else if (segment.includes('*')) {
        const [prefix, suffix] = segment.split('*')
        const next = []
        for (const dir of dirs) {
          if (!fs.existsSync(dir)) continue
          for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            if (!entry.isDirectory()) continue
            if (entry.name.startsWith(prefix) && entry.name.endsWith(suffix)) {
              next.push(path.join(dir, entry.name))
            }
          }
        }
        dirs = next
      } else {
        dirs = dirs.map((dir) => path.join(dir, segment))
      }
    }
    for (const dir of dirs) {
      if (fs.existsSync(path.join(dir, 'package.json'))) out.add(path.relative(root, dir).split(path.sep).join('/'))
    }
  }
  return [...out].sort()
}

/**
 * host face 下真正会产生产物的包：
 * 排除自带 client-only 配置（引用 `clientBundle` / `tsdown.client`）的包——
 * 它们在 host face 下会让 tsdown 报 `No valid configuration found`。
 */
export function expandHostWorkspace(patterns, root = process.cwd()) {
  return expandWorkspacePatterns(patterns, root).filter((pkg) => {
    const own = path.join(root, pkg, 'tsdown.config.ts')
    if (!fs.existsSync(own)) return true
    const text = fs.readFileSync(own, 'utf8')
    return !/clientBundle|tsdown\.client/.test(text)
  })
}

/** 按 `DSH_BUILD_WORKSPACE` 覆盖解析结果；未设置则原样返回模式 */
export function resolveWorkspace(patterns, root = process.cwd()) {
  const override = process.env.DSH_BUILD_WORKSPACE
  if (override && override.trim()) return override.split(',').map((s) => s.trim()).filter(Boolean)
  return patterns
}
