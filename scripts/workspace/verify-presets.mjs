// 离线验证：复用 @deepseek-ai/dsh-agent-presets 真实的 discoverPresets，
// 枚举 shipped preset root，确认 dev-qa / research 被发现且未 broken。
// 与 profile-boot 使用的发现逻辑完全一致，无需启动 web / 浏览器。
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const require = createRequire(import.meta.url)
const repo = resolve(process.argv[2] || '.')
const root = resolve(repo, 'apps/cli/config/agent-presets')

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

const presets = await discoverPresets([{ path: root, trust: 'system' }])
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
