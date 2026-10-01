/** Remove only system-native platform payloads from an owned disposable installation. */
import { existsSync, globSync, rmSync } from 'node:fs'
import { isAbsolute, relative, resolve, sep } from 'node:path'

/**
 * Remove system platform packages, preserving the entry and other optional native dependencies.
 * @param consumerRoot - disposable consumer installation root owned by the caller.
 * @returns Number of removed package directories.
 */
export function removeSystemPlatforms(consumerRoot: string): number {
  const root = resolve(consumerRoot)
  const names = '@deepseek-ai/node-addon-system-{darwin,linux}-{arm64,x64}'
  const paths = globSync([`node_modules/${names}`, `node_modules/**/node_modules/${names}`], { cwd: root })
  for (const path of paths) {
    const absolute = resolve(root, path)
    const owned = relative(root, absolute)
    if (isAbsolute(owned) || owned === '..' || owned.startsWith(`..${sep}`)) throw new Error('release: platform removal escaped consumer')
    rmSync(absolute, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
    if (existsSync(absolute)) throw new Error('release: system platform package remains installed')
  }
  return paths.length
}
