import { expect, it, vi } from 'vitest'
import { credentialKey, credentialRef } from '@deepseek-ai/dsh-credentials'
import { boot } from './model-access-fixture.ts'

it('reads installed dormant providers from real Loader composition without exposing credential values', async () => {
  const { ctx, controller } = await boot()
  await ctx.credentials.modifyRecord(credentialKey('llm-pi-ai', 'openai-codex'), async () => ({ kind: 'grant', payload: { token: 'private-token' } }))
  const value = await controller.configuration()
  expect(value.providers.find(row => row.id === 'openai-codex')).toMatchObject({ configured: false, methods: [{ id: 'oauth' }], record: { configured: true, kind: 'grant' } })
  expect(JSON.stringify(value)).not.toContain('private-token')
})

it('saves and removes a custom provider using revision checks while keeping its key secret', async () => {
  const { controller } = await boot()
  const before = await controller.configuration()
  const request = { provider: 'acme', authentication: 'api-key' as const, apiKey: 'private-api-key', api: 'openai-completions', baseURL: 'https://example.com/v1', models: [{ id: 'model' }], expectedRevision: before.custom!.revision }
  const saved = await controller.save(request)
  const row = saved.providers.find(provider => provider.id === 'acme')!
  expect(row).toMatchObject({ configured: true, credential: { configured: true }, profile: { baseURL: request.baseURL } })
  expect(JSON.stringify(saved)).not.toContain('private-api-key')
  await expect(controller.save(request)).rejects.toMatchObject({ code: 'model-access/rejected', details: { reason: 'conflict' } })
  const removed = await controller.remove({ provider: 'acme', expectedRevision: row.revision })
  expect(removed.providers.find(provider => provider.id === 'acme')).toBeUndefined()
})

it('requires an owner capability and rejects duplicate or stale prompts', async () => {
  const { controller, ctx } = await boot()
  const key = credentialKey('llm-pi-ai', 'test-login')
  ctx.authorization.registerFlow({ key, label: 'Test', methods: [{ id: 'oauth', label: 'Login' }], async run(session) {
    await session.prompt({ kind: 'secret', message: 'Code' })
    await session.commit({ kind: 'grant', payload: { token: 'private-token' } })
  } })
  const owner = controller.start({ provider: 'test-login', method: 'oauth' })
  await vi.waitFor(() => { expect(controller.status(owner).prompt).toBeDefined() })
  expect(() => controller.status({ ...owner, ownerToken: 'wrong' as never })).toThrow('attempt-not-found')
  expect(() => controller.start({ provider: 'test-login', method: 'oauth' })).toThrow('in-flight')
  const promptId = controller.status(owner).prompt!.promptId
  controller.respond({ ...owner, promptId, value: 'private-answer' })
  await vi.waitFor(() => { expect(controller.status(owner).status).toBe('authorized') })
  expect(() => controller.respond({ ...owner, promptId, value: 'again' })).toThrow('stale-prompt')
  expect(JSON.stringify(controller.status(owner))).not.toContain('private-answer')
})

it('withdraws flow-owned losing prompts and contains secret failures', async () => {
  const { controller, ctx } = await boot()
  const withdraw = new AbortController()
  ctx.authorization.registerFlow({ key: credentialKey('llm-pi-ai', 'test-race'), label: 'Race', methods: [{ id: 'oauth', label: 'Login' }], async run(session) {
    await session.prompt({ kind: 'text', message: 'Paste callback', signal: withdraw.signal }).catch(() => {})
    throw new Error('private-token leaked upstream')
  } })
  const owner = controller.start({ provider: 'test-race', method: 'oauth' })
  await vi.waitFor(() => { expect(controller.status(owner).prompt).toBeDefined() })
  withdraw.abort()
  await vi.waitFor(() => { expect(controller.status(owner).status).toBe('failed') })
  expect(controller.status(owner)).toMatchObject({ errorCode: 'authorization-failed' })
  expect(JSON.stringify(controller.status(owner))).not.toContain('private-token')
  expect(controller.status(owner).prompt).toBeUndefined()
})

it('cancels ownership-scoped authorization without saving a credential', async () => {
  const { controller, ctx } = await boot()
  const key = credentialKey('llm-pi-ai', 'test-cancel')
  ctx.authorization.registerFlow({ key, label: 'Cancel', methods: [{ id: 'oauth', label: 'Login' }], async run(session) { await session.prompt({ kind: 'text', message: 'Wait' }); await session.commit({ kind: 'grant', payload: 'private' }) } })
  const owner = controller.start({ provider: 'test-cancel', method: 'oauth' })
  controller.cancel(owner)
  await vi.waitFor(() => { expect(controller.status(owner).status).toBe('cancelled') })
  expect(await ctx.credentials.describeRecord(key)).toMatchObject({ configured: false })
})

it('expires attempts and bounds retained results without leaking a stale owner', async () => {
  const { controller, ctx } = await boot({ attemptTtlMs: 1000, retentionMs: 1000, maxAttempts: 1 })
  const key = credentialKey('llm-pi-ai', 'test-expire')
  ctx.authorization.registerFlow({ key, label: 'Expire', methods: [{ id: 'oauth', label: 'Login' }], async run(session) { await session.prompt({ kind: 'text', message: 'Wait' }) } })
  vi.useFakeTimers()
  try {
    const owner = controller.start({ provider: 'test-expire', method: 'oauth' })
    await vi.advanceTimersByTimeAsync(1001)
    expect(controller.status(owner)).toMatchObject({ status: 'cancelled' })
    expect(() => controller.start({ provider: 'test-expire', method: 'oauth' })).toThrow('attempt-limit')
    await vi.advanceTimersByTimeAsync(1001)
    expect(() => controller.status(owner)).toThrow('attempt-not-found')
    const next = controller.start({ provider: 'test-expire', method: 'oauth' })
    controller.cancel(next)
    await vi.advanceTimersByTimeAsync(0)
  } finally { vi.useRealTimers() }
})

it('serializes competing saves before a stale writer can change credentials', async () => {
  const { controller } = await boot()
  const before = await controller.configuration()
  const request = { provider: 'custom-race', authentication: 'api-key' as const, apiKey: 'key-one', api: 'openai-completions', baseURL: 'https://example.com/v1', models: [{ id: 'model' }], expectedRevision: before.custom!.revision }
  const results = await Promise.allSettled([controller.save(request), controller.save({ ...request, apiKey: 'key-two' })])
  expect(results.map(result => result.status)).toEqual(['fulfilled', 'rejected'])
})

it('keeps logout and API key removal separate and reports remaining sources', async () => {
  const { controller, ctx } = await boot()
  const before = await controller.configuration()
  await controller.save({ provider: 'openai', expectedRevision: before.custom!.revision, authentication: 'api-key', apiKey: 'private-key' })
  const key = credentialKey('llm-pi-ai', 'openai')
  await ctx.credentials.modifyRecord(key, async () => ({ kind: 'grant', payload: 'private-token' }))
  const loggedOut = await controller.logout({ provider: 'openai' })
  expect(loggedOut.providers.find(row => row.id === 'openai')).toMatchObject({ record: { configured: false }, credential: { configured: true } })
  const cleared = await controller.clearKey({ provider: 'openai' })
  expect(cleared.providers.find(row => row.id === 'openai')).toMatchObject({ credential: { configured: false } })
})

it('verifies a fixed small request and returns no model text or unsafe failure message', async () => {
  const { controller, ctx } = await boot()
  const stream = vi.spyOn(ctx.llm, 'stream').mockImplementation(async function* (request) {
    expect(request).toMatchObject({ provider: 'openai', model: 'test', maxTokens: 16, messages: [{ role: 'user', content: [{ type: 'text', text: 'Reply OK.' }] }] })
    yield { type: 'finish', reason: { kind: 'stop' } }
  })
  expect(await controller.verify({ provider: 'openai', model: 'test' }, new AbortController().signal)).toEqual({ ok: true })
  stream.mockImplementation(async function* () { throw new Error('private-credential') })
  expect(await controller.verify({ provider: 'openai', model: 'test' }, new AbortController().signal)).toEqual({ ok: false, errorCode: 'verification-failed' })
})

it('reports a partially committed profile without exposing credential refusal text', async () => {
  const { controller, ctx } = await boot()
  const before = await controller.configuration()
  await ctx.credentials.set(credentialRef('OLD_KEY'), 'old-private-key')
  await ctx.settings.mutate(before.custom!.settingsNs, [{ op: 'set', path: ['providers', 'openai'], value: { apiKeyEnv: 'OLD_KEY' } }], before.custom!.revision)
  const current = (await controller.configuration()).providers.find(row => row.id === 'openai')!
  vi.spyOn(ctx.credentials, 'set').mockRejectedValue(new Error('private-api-key'))
  await expect(controller.save({ provider: 'openai', expectedRevision: current.revision, authentication: 'api-key', apiKey: 'private-api-key' }))
    .rejects.toMatchObject({ code: 'model-access/rejected', message: 'Model access: configuration-saved-credential-failed' })
  const changed = (await controller.configuration()).providers.find(row => row.id === 'openai')!
  expect(changed).toMatchObject({ profile: { apiKeyEnv: 'DSH_MODEL_OPENAI_API_KEY' }, credential: { configured: false } })
  expect(changed.revision).toBeGreaterThan(current.revision)
  expect(await ctx.credentials.describe(credentialRef('OLD_KEY'))).toMatchObject({ configured: true })
})

it('reports an admitted credential commit failure after cancel as failed rather than cancelled', async () => {
  const { controller, ctx } = await boot()
  const admitted = Promise.withResolvers<undefined>()
  const storage = Promise.withResolvers<never>()
  vi.spyOn(ctx.credentials, 'modifyRecord').mockImplementationOnce(() => {
    admitted.resolve(undefined)
    return storage.promise
  })
  ctx.authorization.registerFlow({
    key: credentialKey('llm-pi-ai', 'test-commit-failure'), label: 'Commit', methods: [{ id: 'oauth', label: 'Login' }],
    async run(session) { await session.commit({ kind: 'grant', payload: 'private-token' }) },
  })
  const owner = controller.start({ provider: 'test-commit-failure', method: 'oauth' })
  await admitted.promise
  expect(controller.cancel(owner).status).toBe('pending')
  storage.reject(new Error('private-storage-token'))
  await vi.waitFor(() => { expect(controller.status(owner).status).toBe('failed') })
  expect(controller.status(owner)).toMatchObject({ errorCode: 'authorization-failed' })
  expect(JSON.stringify(controller.status(owner))).not.toContain('private-storage-token')
})

it('keeps the verification URL and device code when a flow reports progress', async () => {
  const { controller, ctx } = await boot()
  ctx.authorization.registerFlow({
    key: credentialKey('llm-pi-ai', 'test-notice'), label: 'Notice', methods: [{ id: 'oauth', label: 'Login' }],
    async run(session) {
      session.notify({ message: 'Enter this code', url: 'https://example.com/device', code: 'ABCD' })
      session.notify({ message: 'Waiting for confirmation' })
      await session.prompt({ kind: 'text', message: 'Continue' })
    },
  })
  const owner = controller.start({ provider: 'test-notice', method: 'oauth' })
  expect(controller.status(owner).notice).toEqual({ message: 'Waiting for confirmation', url: 'https://example.com/device', code: 'ABCD' })
  controller.cancel(owner)
})

it('activates official OAuth after removing user endpoint overrides and refuses inherited overrides', async () => {
  const { controller, ctx } = await boot()
  const before = await controller.configuration()
  const ns = before.custom!.settingsNs
  await ctx.settings.mutate(ns, [{ op: 'set', path: ['providers', 'openai-codex'], value: {
    apiKeyEnv: 'TEST_KEY', baseURL: 'https://example.com/v1',
  } }], before.custom!.revision)
  await ctx.credentials.modifyRecord(credentialKey('llm-pi-ai', 'openai-codex'), async () => ({ kind: 'grant', payload: 'private-token' }))
  const row = (await controller.configuration()).providers.find(provider => provider.id === 'openai-codex')!
  const activated = await controller.save({ provider: row.id, expectedRevision: row.revision, authentication: 'oauth' })
  const current = activated.providers.find(provider => provider.id === row.id)!
  expect(current).toMatchObject({ configured: true, authentication: 'oauth' })
  expect(current.profile.baseURL).toBeUndefined()
  expect(current.profile.apiKeyEnv).toBeUndefined()
  const describe = ctx.settings.describe.bind(ctx.settings)
  vi.spyOn(ctx.settings, 'describe').mockImplementation(options => describe(options).map(descriptor => descriptor.ns !== ns ? descriptor : {
    ...descriptor, base: { providers: { 'openai-codex': { baseURL: 'https://example.com/unsafe' } } },
  }))
  await expect(controller.save({ provider: row.id, expectedRevision: current.revision, authentication: 'oauth' }))
    .rejects.toMatchObject({ details: { reason: 'subscription-base-override' } })
})

it('rejects custom OAuth payloads and keeps a key when its form field is left blank', async () => {
  const { controller, ctx } = await boot()
  const before = await controller.configuration()
  const saved = await controller.save({ provider: 'openai', expectedRevision: before.custom!.revision, authentication: 'api-key', apiKey: 'private-key' })
  const row = saved.providers.find(provider => provider.id === 'openai')!
  const next = await controller.save({ provider: 'openai', expectedRevision: row.revision, authentication: 'api-key', displayName: 'Renamed' })
  expect(next.providers.find(provider => provider.id === 'openai')).toMatchObject({ credential: { configured: true }, profile: { displayName: 'Renamed' } })
  await ctx.credentials.modifyRecord(credentialKey('llm-pi-ai', 'openai-codex'), async () => ({ kind: 'grant', payload: 'private-token' }))
  await expect(controller.save({ provider: 'openai-codex', expectedRevision: next.custom!.revision, authentication: 'oauth', baseURL: 'https://example.com/v1' }))
    .rejects.toMatchObject({ details: { reason: 'subscription-custom-endpoint' } })
})

it('delegates discovery and projects only IDs and names without returning provider metadata', async () => {
  const { controller, ctx } = await boot()
  const discover = vi.spyOn(ctx.llm, 'discoverModels').mockResolvedValue([{ id: 'model', name: 'Model', diagnostic: 'private-token' }])
  const signal = new AbortController().signal
  expect(await controller.discover({ baseURL: 'https://example.com/v1', api: 'openai-completions' }, signal)).toEqual([{ id: 'model', name: 'Model' }])
  expect(discover).toHaveBeenCalledWith('first', { baseURL: 'https://example.com/v1', api: 'openai-completions' }, signal)
})
