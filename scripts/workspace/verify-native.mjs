import { createRequire } from 'node:module'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { fileURLToPath, pathToFileURL } from 'node:url'
import fs from 'node:fs'
import path from 'node:path'

const execFileP = promisify(execFile)
const here = path.dirname(fileURLToPath(import.meta.url))

// 定位 repo 根：参数 > scripts/../my-deepseek-harness/deepseek-harness
function resolveRepoRoot() {
  const arg = process.argv[2]
  if (arg) return path.resolve(arg)
  return path.resolve(here, '..', 'my-deepseek-harness', 'deepseek-harness')
}
const REPO = resolveRepoRoot()
const pnpm = path.join(REPO, 'node_modules', '.pnpm')

if (!fs.existsSync(REPO)) {
  console.error('repo root 不存在: ' + REPO)
  process.exit(2)
}

// 收集所有 workspace 包目录（按包名索引）
function collectPkgDirs() {
  const map = new Map()
  function walk(p) {
    if (!fs.existsSync(p)) return
    for (const e of fs.readdirSync(p, { withFileTypes: true })) {
      if (!e.isDirectory()) continue
      const d = path.join(p, e.name)
      if (e.name === 'node_modules' || e.name === '.pnpm') continue
      const pj = path.join(d, 'package.json')
      if (fs.existsSync(pj)) {
        let m; try { m = JSON.parse(fs.readFileSync(pj, 'utf8')) } catch { m = {} }
        if (m.name) map.set(m.name, d)
      }
      walk(d)
    }
  }
  for (const r of ['packages', 'apps', 'vendor']) walk(path.join(REPO, r))
  return map
}
const PKG_DIRS = collectPkgDirs()

function reqAt(dir, name) {
  const req = createRequire(path.join(dir, '_anchor.js'))
  try { return req(name) } catch { return null }
}

// 从 consumer 目录（按包名）require 原生包
function reqFromConsumer(consumerPkgName, nativeName) {
  const dir = PKG_DIRS.get(consumerPkgName)
  if (!dir) return { error: 'consumer 包未找到: ' + consumerPkgName, dir: null, pkg: null }
  const pkg = reqAt(dir, nativeName)
  if (pkg === null) return { error: '从 ' + dir + ' 无法 require ' + nativeName, dir, pkg: null }
  return { dir, pkg, error: null }
}

const results = []
function report(name, pass, detail) {
  results.push({ name, pass, detail })
  console.log((pass ? 'PASS' : 'FAIL') + '  ' + name + (detail ? '  — ' + detail : ''))
}

async function check(name, fn) {
  const t0 = Date.now()
  try {
    const d = await fn()
    report(name, true, d ? d + ' (' + (Date.now() - t0) + 'ms)' : '(' + (Date.now() - t0) + 'ms)')
  } catch (e) {
    report(name, false, (e && (e.code || e.message)) ? String(e.code || '') + ' ' + String(e.message).split('\n')[0] : String(e))
  }
}

// 从 .pnpm store 绝对路径动态 import（用于构建工具，其 consumer 是第三方包）
async function importStore(pkgPrefix, innerRel) {
  const dirs = fs.readdirSync(pnpm).filter(d => d.startsWith(pkgPrefix + '@'))
  if (!dirs.length) throw new Error('store 无 ' + pkgPrefix)
  const mod = path.join(pnpm, dirs[0], 'node_modules', innerRel)
  if (!fs.existsSync(mod)) throw new Error('store 路径缺失: ' + mod)
  return import(pathToFileURL(mod).href)
}

// 1. sharp —— 实际创建+编码 PNG（consumer: attachment-local）
await check('sharp 图片处理（创建+编码 PNG）', async () => {
  const r = reqFromConsumer('@deepseek-ai/dsh-attachment-local', 'sharp')
  if (r.error) throw new Error(r.error)
  const buf = await r.pkg({ create: { width: 16, height: 16, channels: 4, background: { r: 255, g: 0, b: 0, alpha: 1 } } }).png().toBuffer()
  if (!buf || buf.length < 10) throw new Error('空输出')
  return 'png ' + buf.length + 'B, sharp ' + r.pkg.versions.sharp
})

// 2. ripgrep —— 实际搜索文件（consumer: tool-fs-search）
await check('ripgrep 文件搜索（真实匹配）', async () => {
  const r = reqFromConsumer('@deepseek-ai/dsh-tool-fs-search', '@vscode/ripgrep')
  if (r.error) throw new Error(r.error)
  const tmp = path.join(REPO, 'outputs', '_rg_verify_' + Date.now() + '.txt')
  fs.mkdirSync(path.dirname(tmp), { recursive: true })
  fs.writeFileSync(tmp, 'hello world\ncore-ok-line\n')
  try {
    const { stdout } = await execFileP(r.pkg.rgPath, ['core-ok-line', tmp])
    if (!String(stdout).includes('core-ok-line')) throw new Error('无匹配: ' + String(stdout))
    return 'matched "' + String(stdout).trim() + '"'
  } finally { fs.rmSync(tmp, { force: true }) }
})

// 3. node-pty —— 实际 spawn cmd 执行命令（consumer: subprocess-local）
await check('node-pty 终端执行（spawn cmd echo）', async () => {
  const r = reqFromConsumer('@deepseek-ai/dsh-subprocess-local', 'node-pty')
  if (r.error) throw new Error(r.error)
  const out = await new Promise((resolve, reject) => {
    let buf = ''
    const p = r.pkg.spawn('cmd.exe', ['/c', 'echo pty-ok'], { cols: 80, rows: 24, cwd: process.cwd(), env: process.env })
    p.onData(d => { buf += d })
    p.onExit(({ exitCode }) => exitCode === 0 ? resolve(buf) : reject(new Error('exit ' + exitCode)))
    setTimeout(() => reject(new Error('timeout')), 8000)
  })
  if (!out.includes('pty-ok')) throw new Error('无 pty-ok: ' + JSON.stringify(out))
  return 'cmd echo pty-ok → ok'
})

// 4. koffi —— 实际 FFI 调用 kernel32 GetTickCount（consumer: fs-local）
await check('koffi FFI 调用（kernel32 GetTickCount）', async () => {
  const r = reqFromConsumer('@deepseek-ai/dsh-fs-local', 'koffi')
  if (r.error) throw new Error(r.error)
  const lib = r.pkg.load('kernel32.dll')
  const fn = lib.func('__stdcall', 'GetTickCount', 'uint32', [])
  const a = fn(), b = fn()
  if (typeof b !== 'number') throw new Error('返回值非数字')
  return 'GetTickCount = ' + b + ' (两次 diff ' + (b - a) + 'ms)'
})

// 5. esbuild —— 实际 transform TS（从 store 绝对路径）
await check('esbuild transform TS', async () => {
  const esbuild = (await importStore('esbuild', 'esbuild/lib/main.js')).default ?? (await importStore('esbuild', 'esbuild/lib/main.js'))
  const r = await esbuild.transform('const x: number = 1 + 2; console.log(x)', { loader: 'ts' })
  if (!r.code.includes('3') && !r.code.includes('1 + 2')) throw new Error('输出异常')
  return 'esbuild ' + esbuild.version
})

// 6. lightningcss —— 实际 transform CSS（从根或 vite 位置解析）
await check('lightningcss transform CSS', async () => {
  let transform = null
  // 先试根
  const rootLc = reqAt(REPO, 'lightningcss')
  if (rootLc && typeof rootLc.transform === 'function') transform = rootLc.transform
  else {
    // 回退：从 vite@6 的位置 import lightningcss（vite build 依赖它）
    const viteDir = fs.readdirSync(pnpm).find(d => d.startsWith('vite@6'))
    if (viteDir) {
      const lc = reqAt(path.join(pnpm, viteDir, 'node_modules', 'vite'), 'lightningcss')
      if (lc && typeof lc.transform === 'function') transform = lc.transform
    }
  }
  if (typeof transform !== 'function') throw new Error('无法解析 lightningcss.transform')
  const r = transform({ code: Buffer.from('.a{color:red}'), minify: true })
  if (!r.code.toString().includes('red')) throw new Error('输出异常')
  return 'minified "' + r.code.toString().trim() + '"'
})

console.log('\n==== 汇总 ====')
const fails = results.filter(r => !r.pass)
console.log('PASS ' + (results.length - fails.length) + '/' + results.length)
if (fails.length) { console.log('失败项:'); fails.forEach(f => console.log('  - ' + f.name + ': ' + f.detail)) }
process.exit(fails.length ? 1 : 0)
