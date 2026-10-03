/** Built wire acceptance; requires the settings-controller Host Typert artifact. */
import { expect, it, vi } from 'vitest'
import { z } from 'zod'
import { credentialKey } from '@deepseek-ai/dsh-credentials'
import { apply as applyConnection } from '../../../client/connection/src/index.ts'
import TypertRegistry from '../../../typert/registry/src/index.ts'
import TypertGateway from '../../gateway/src/index.ts'
import { validateTypertManifest } from '../../../typert/loader/src/index.ts'
import { boot } from './model-access-fixture.ts'

it('dispatches model access through the real Connection fetch handler and generated strict gateway contract', async () => {
  const { ctx } = await boot()
  await applyConnection(ctx)
  await ctx.plugin(TypertRegistry)
  await ctx.plugin(TypertGateway)
  // The build-generated descriptor is the deployed wire contract, not a hand-written test mirror.
  const descriptorUrl = new URL('../lib/typert.host.js', import.meta.url).href
  const descriptor: unknown = await import(/* @vite-ignore */ descriptorUrl)
  if (typeof descriptor !== 'object' || descriptor === null || !('TYPERT' in descriptor)) throw new Error('missing descriptor')
  ctx.typert.register(validateTypertManifest('@deepseek-ai/dsh-api-settings-controller', descriptor.TYPERT))
  const handler = ctx.connection.createSharedFetchHandler('/api')
  async function rpc(method: string, args: object = {}) {
    const response = await handler.fetch(new Request(`http://localhost/api/modelAccess/${method}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'client-request', rpcId: 'model-access-wire', method: `modelAccess/${method}`, payload: { args } }),
    }))
    expect(response.status).toBe(200)
    const body: unknown = await response.json()
    expect(body).toMatchObject({ type: 'server-response', rpcId: 'model-access-wire' })
    return z.object({ result: z.unknown() }).parse(body).result
  }
  const configuration = await rpc('configuration')
  expect(configuration).toMatchObject({ ok: true })
  expect(z.object({ value: z.object({ providers: z.array(z.unknown()) }) }).parse(configuration).value.providers.length).toBeGreaterThan(0)
  const key = credentialKey('llm-pi-ai', 'wire-login')
  ctx.authorization.registerFlow({ key, label: 'Wire login', methods: [{ id: 'oauth', label: 'Login' }], async run(session) {
    const code = await session.prompt({ kind: 'secret', message: 'Code' })
    expect(code).toBe('private-wire-answer')
    await session.commit({ kind: 'grant', payload: { token: 'private-wire-grant' } })
  } })
  const start = z.object({ ok: z.literal(true), value: z.object({ attemptId: z.string(), ownerToken: z.string() }) })
    .parse(await rpc('start', { request: { provider: 'wire-login', method: 'oauth' } }))
  const owner = start.value
  const statusSchema = z.object({ ok: z.literal(true), value: z.object({
    status: z.string(), prompt: z.object({ promptId: z.string() }).optional(),
  }) })
  let status = statusSchema.parse(await rpc('status', { request: owner }))
  await vi.waitFor(async () => {
    status = statusSchema.parse(await rpc('status', { request: owner }))
    expect(status.value.prompt).toBeDefined()
  })
  expect(await rpc('respond', { request: {
    ...owner, promptId: status.value.prompt!.promptId, value: 'private-wire-answer',
  } })).toMatchObject({ ok: true })
  await vi.waitFor(async () => {
    status = statusSchema.parse(await rpc('status', { request: owner }))
    expect(status.value.status).toBe('authorized')
  })
  expect(JSON.stringify(status)).not.toMatch(/private-wire-(answer|grant)/)
  const wrongOwner = await rpc('status', { request: { ...owner, ownerToken: 'wrong' } })
  expect(wrongOwner).toMatchObject({ ok: false, error: { code: 'model-access/rejected', details: { reason: 'attempt-not-found' } } })
  expect(await rpc('cancel', { request: owner })).toMatchObject({ ok: true, value: { status: 'authorized' } })
})
