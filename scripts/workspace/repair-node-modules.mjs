import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * repair-node-modules.mjs —— 修复 deepseek-harness 在 Windows 下 `pnpm install --no-optional`
 * 或中断安装导致的 node_modules 链接缺失。
 *
 * 背景：
 *   - `pnpm install --no-optional` 会跳过 optional 平台包（esbuild/rolldown/lightningcss/sharp/
 *     koffi/node-pty/ripgrep 等的 win32-x64 binding），手动补装只把包放进 store、不建 per-package 链接。
 *   - 链接缺失导致两类故障：① esbuild "Host version X does not match binary version Y"（vite/tsdown 构建失败）；
 *     ② 运行时 `Cannot find package 'ws'/'fflate'/'zod'/'koffi'`（web server / headless 启动失败）。
 *
 * 用法：node repair-node-modules.mjs [repoRoot]
 *   repoRoot 默认 <本脚本>/../my-deepseek-harness/deepseek-harness
 *
 * 幂等：已存在的链接跳过；只补缺失。可反复执行。
 */

const here = path.dirname(fileURLToPath(import.meta.url))
function resolveRepoRoot() {
  const arg = process.argv[2]
  if (arg) return path.resolve(arg)
  return path.resolve(here, '..', 'my-deepseek-harness', 'deepseek-harness')
}
const REPO = resolveRepoRoot()
const pnpm = path.join(REPO, 'node_modules', '.pnpm')

if (!fs.existsSync(pnpm)) {
  console.error('node_modules/.pnpm 不存在，请先 pnpm install: ' + pnpm)
  process.exit(2)
}

let fixed = 0, skipped = 0, missing = 0
const missingList = []
const fixedList = []

function link(src, dst) {
  if (fs.existsSync(dst)) { skipped++; return 'skip' }
  if (!fs.existsSync(src)) { missing++; missingList.push('src-missing: ' + src); return 'miss' }
  fs.mkdirSync(path.dirname(dst), { recursive: true })
  try {
    fs.symlinkSync(src, dst, 'junction')
    fixed++; fixedList.push(dst.replace(REPO, '') + '  <-  ' + src.replace(REPO, ''))
    return 'fixed'
  } catch (e) {
    missing++; missingList.push('link-fail: ' + dst + ' ' + e.message)
    return 'miss'
  }
}

// 从 store 解析包目录（scoped 包用 + 分隔）；返回 store 里 node_modules/<name> 的绝对路径
function storePath(name, versionHint) {
  const dirName = name.replace('/', '+')
  const cands = fs.readdirSync(pnpm).filter(d => d.startsWith(dirName + '@'))
  if (!cands.length) return null
  // 有精确版本提示就优先精确匹配，否则取字典序最大（store 里通常唯一）
  let chosen
  if (versionHint) {
    chosen = cands.find(d => d === dirName + '@' + versionHint)
    if (!chosen) chosen = cands.filter(d => d.startsWith(dirName + '@' + versionHint)).sort().pop()
  }
  if (!chosen) chosen = cands.sort().pop()
  return path.join(pnpm, chosen, 'node_modules', name)
}

console.log('== 修复 esbuild 的 @esbuild/win32-x64 binding（--no-optional 常见遗漏）==')
// esbuild@X 需要 node_modules/esbuild/node_modules/@esbuild/win32-x64 -> @esbuild+win32-x64@X
for (const d of fs.readdirSync(pnpm)) {
  if (!d.startsWith('esbuild@')) continue
  const ver = d.slice('esbuild@'.length)
  const src = storePath('@esbuild/win32-x64', ver)
  if (!src) { missing++; missingList.push('esbuild ' + ver + ' 无对应 @esbuild/win32-x64'); continue }
  const dst = path.join(pnpm, d, 'node_modules', 'esbuild', 'node_modules', '@esbuild', 'win32-x64')
  link(src, dst)
}

console.log('== 修复 workspace 包直接依赖的链接 ==')
function walk(p) {
  for (const e of fs.readdirSync(p, { withFileTypes: true })) {
    if (!e.isDirectory()) continue
    const d = path.join(p, e.name)
    if (e.name === 'node_modules' || e.name === '.pnpm') continue
    const pj = path.join(d, 'package.json')
    if (fs.existsSync(pj)) {
      let m; try { m = JSON.parse(fs.readFileSync(pj, 'utf8')) } catch { m = {} }
      if (m.name) {
        const all = { ...(m.dependencies || {}), ...(m.optionalDependencies || {}) }
        const nm = path.join(d, 'node_modules')
        fs.mkdirSync(nm, { recursive: true })
        for (const [dep] of Object.entries(all)) {
          if (dep.startsWith('@deepseek-ai/')) continue // workspace 内部，走根 junction
          const dst = path.join(nm, dep)
          if (fs.existsSync(dst)) { skipped++; continue }
          const src = storePath(dep)
          if (!src) { missing++; missingList.push('store 无 ' + dep + ' (需下载，来自 ' + m.name + ')'); continue }
          link(src, dst)
        }
      }
    }
    walk(d)
  }
}
for (const r of ['packages', 'apps', 'vendor']) {
  const a = path.join(REPO, r)
  if (fs.existsSync(a)) walk(a)
}

console.log('\n== 结果 ==')
console.log('fixed=' + fixed + '  skipped(已存在)=' + skipped + '  missing=' + missing)
if (fixedList.length) {
  console.log('新建链接:')
  for (const l of fixedList.slice(0, 60)) console.log('  + ' + l)
  if (fixedList.length > 60) console.log('  ... 共 ' + fixedList.length + ' 条')
}
if (missingList.length) {
  console.log('无法修复(需重新下载到 store):')
  for (const m of [...new Set(missingList)].slice(0, 40)) console.log('  ! ' + m)
}
process.exit(missing ? 1 : 0)
