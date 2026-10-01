/** Disposable-install platform removal must preserve unrelated optional payloads. */
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { expect, it } from 'vitest'
import { removeSystemPlatforms } from './optional-system-platforms.ts'

it('removes root and nested system platforms while preserving Koffi and the system entry', (test) => {
  const root = mkdtempSync(join(tmpdir(), 'dsh-optional-platforms-'))
  test.onTestFinished(() => { rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }) })
  const payloads = [
    'node_modules/@deepseek-ai/node-addon-system-linux-x64',
    'node_modules/consumer/node_modules/@deepseek-ai/node-addon-system-darwin-arm64',
    'node_modules/@deepseek-ai/node-addon-system',
    'node_modules/@koromix/koffi-linux-x64',
    'node_modules/koffi',
  ]
  for (const path of payloads) {
    mkdirSync(join(root, path), { recursive: true })
    writeFileSync(join(root, path, 'package.json'), '{}')
  }
  expect(removeSystemPlatforms(root)).toBe(2)
  for (const [index, path] of payloads.entries()) expect(existsSync(join(root, path))).toBe(index >= 2)
  expect(removeSystemPlatforms(root)).toBe(0)
})
