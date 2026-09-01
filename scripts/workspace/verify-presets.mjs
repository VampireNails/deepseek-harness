// 离线验证：复用 @deepseek-ai/dsh-agent-presets 真实的 discoverPresets，
// 枚举 shipped preset root，确认 dev-qa / research 被发现且未 broken。
// 与 profile-boot 使用的发现逻辑完全一致，无需启动 web / 浏览器。
//
// preset root 自 v0.1.2-alpha.3 起由 apps/cli/config/agent-presets 迁至
// packages/preset/agent-presets/presets（CLI 不再注入 SHIPPED_PRESET_ROOT，
// 改由包内 discovery 自解析 ../presets/）。
import { createRequire } from 'node:module'
import { resolve, join } from 'node:path'
import { homedir } from 'node:os'
import { pathToFileURL } from 'node:url'

const require = createRequire(import.meta.url)
const repo = resolve(process.argv[2] || '.')
const root = resolve(repo, 'packages/preset/agent-presets/presets')

const PRESET_LIB = resolve(repo, 'packages/preset/agent-presets/lib/index.js')
let mod
try {
  mod = await import(pathToFileURL(PRESET_LIB).href)
} catch (e) {
  console.error('IMPORT_FAIL', e.message)
  process.exit(2)
}
const discoverPresets = mod.discoverPresets ?? mod.default?.discoverPresets
if (typeof discoverPresets !== 'function') {
  console.error('NO_EXPORT discoverPresets; keys=', Object.keys(mod))
  process.exit(3)
}

// `harnessBase` (2nd arg since v0.1.2-alpha.3) stands in for the installed
// harness: a row's package name resolves from here, and this directory's
// upward `node_modules` walk reaches the workspace. It must be a directory
// file:// URL -- a bare path throws ERR_INVALID_URL_SCHEME.
//
// It MUST be a profile directory, not the repo root. `packageInstalled` looks
// for `node_modules/<pkg>/package.json` walking upward from here, and the repo
// root never self-links its own workspace source packages (dsh-persona,
// dsh-tool-fs, ...), so passing the repo root reports every preset BROKEN --
// a false alarm. From a profile the walk reaches the shared plugin pool that
// `healProfilesModuleFallback` maintains at $DSH_HOME/profiles/node_modules.
const home = process.env.USERPROFILE || process.env.HOME || homedir()
const profileDir = process.argv[3]
  ? resolve(process.argv[3])
  : resolve(home, '.dsh', 'profiles', 'headless')
const HARNESS = new URL('.', pathToFileURL(join(profileDir, 'package.json'))).href
console.log(`HARNESS_BASE=${HARNESS}`)
const presets = await discoverPresets([{ path: root, trust: 'system' }], HARNESS)
console.log(`PRESET_ROOT=${root}`)
console.log(`DISCOVERED_COUNT=${presets.length}`)
const want = new Set(['dev-qa', 'research'])
let ok = 0
for (const p of presets) {
  const mark = p.broken === undefined ? 'OK ' : 'BROKEN'
  const hit = want.has(p.id) ? ' <<<' : ''
  console.log(
    `[${mark}] id=${p.id} trust=${p.trust} order=${p.order ?? '-'} ` +
    `name=${JSON.stringify(p.name ?? '')} broken=${p.broken ?? 'none'}${hit}`
  )
  if (want.has(p.id) && p.broken === undefined) ok++
}
console.log(`TARGET_HITS=${ok}/${want.size}`)
process.exit(ok === want.size ? 0 : 4)
