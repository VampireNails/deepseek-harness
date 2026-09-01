import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * fix-dsh-node-modules.mjs —— 一键修复 + 校验 deepseek-harness 的 node_modules 链接。
 *
 * 用途：deepseek-harness 上游迭代后，重新 `pnpm install`（尤其 `--no-optional`）或安装中断，
 * 会留下原生包/依赖的链接缺失（esbuild binding 错版、ws/fflate/zod/koffi 等缺链、ui-primitives
 * 的 markdown 渲染依赖缺链等）。跑一次本脚本即可：① 补全链接；② 功能级校验（不是假加载）。
 *
 * 用法：node fix-dsh-node-modules.mjs [repoRoot]
 *   repoRoot 默认 <本脚本>/../my-deepseek-harness/deepseek-harness
 *
 * 子脚本：
 *   - repair-node-modules.mjs：补 esbuild binding + workspace 直接依赖的 junction（幂等）
 *   - verify-native.mjs      ：功能级验证 sharp/ripgrep/node-pty/koffi/esbuild/lightningcss（真实执行）
 */

const here = path.dirname(fileURLToPath(import.meta.url))
const node = process.execPath
const repoArg = process.argv[2]

function run(label, script) {
  console.log('\n========== ' + label + ' ==========')
  const args = [script]
  if (repoArg) args.push(repoArg)
  const r = spawnSync(node, args, { stdio: 'inherit', cwd: here, env: { ...process.env, NODE_OPTIONS: '' } })
  return r.status ?? 1
}

const rc1 = run('[1/2] 修复链接 repair-node-modules.mjs', path.join(here, 'repair-node-modules.mjs'))
const rc2 = run('[2/2] 功能校验 verify-native.mjs', path.join(here, 'verify-native.mjs'))

console.log('\n========== 总结 ==========')
console.log('修复链接 : ' + (rc1 === 0 ? 'OK' : 'FAIL(exit ' + rc1 + ')'))
console.log('功能校验 : ' + (rc2 === 0 ? 'OK（原生功能全部真实可用）' : 'FAIL(exit ' + rc2 + ')'))
process.exit(rc1 || rc2 ? 1 : 0)
