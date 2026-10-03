import { afterEach, describe, expect, it, vi } from 'vitest'
import { catalogProvider } from '../src/catalog.ts'
import type { Api, Model } from '@earendil-works/pi-ai'
import { resolveProfiles } from '../src/config.ts'
import type { PiAiProviderProfile } from '../src/config.ts'
import { buildProvider } from '../src/provider.ts'
import { createModels } from '../src/models.ts'
import { memoryAuth } from './auth-double.ts'
import { PiAiAdapter } from '../src/adapter.ts'

afterEach(() => { vi.restoreAllMocks() })

/** Resolve auth through pi-ai's real execution path, mocking only provider OAuth network work. */
function route(profile: PiAiProviderProfile, expires = Date.now() + 3_600_000) {
  const provider = catalogProvider('anthropic')!
  const oauth = provider.auth.oauth!
  const refresh = vi.spyOn(oauth, 'refresh').mockImplementation(credential => Promise.resolve(credential))
  const toAuth = vi.spyOn(oauth, 'toAuth').mockResolvedValue({ apiKey: 'subscription-token' })
  const auth = memoryAuth({ anthropic: {
    type: 'oauth', access: 'subscription-token', refresh: 'refresh-token', expires,
  } })
  const models = createModels(auth)
  models.setProvider(resolveProfiles({ anthropic: profile }).get('anthropic')!.piProvider!)
  return { models, refresh, toAuth, auth }
}

describe('OAuth route destination enforcement', () => {
  it.each([
    ['endpoint', { baseURL: 'https://custom.invalid/v1' }],
    ['protocol', { api: 'openai-completions' }],
    ['headers', { headers: { 'X-Proxy-Target': 'custom.invalid' } }],
  ] as const)('refuses an official grant on a route with changed %s before deriving request auth', async (_label, profile) => {
    const { models, refresh, toAuth } = route(profile)
    await expect(models.getAuth('anthropic')).rejects.toThrow(/OAuth.*native.*route/)
    expect(refresh).not.toHaveBeenCalled()
    expect(toAuth).not.toHaveBeenCalled()
  })

  it.each([
    ['endpoint', { baseUrl: 'https://model-proxy.invalid' }],
    ['protocol', { api: 'openai-completions' }],
    ['headers', { headers: { 'X-Proxy-Target': 'model-proxy.invalid' } }],
  ] as const)('checks materialized model %s rather than only provider-level metadata', async (_label, override) => {
    const { models, toAuth } = route({})
    const native = catalogProvider('anthropic')!.getModels()[0]!
    const changed: Model<Api> = { ...native, ...override }
    models.setProvider(buildProvider({
      provider: 'anthropic', displayName: 'anthropic', namesCredential: false, models: [changed],
    }))
    await expect(models.getAuth('anthropic')).rejects.toThrow(/OAuth.*native.*route/)
    expect(toAuth).not.toHaveBeenCalled()
  })

  it('refuses changed routes before refreshing an expired grant', async () => {
    const { models, refresh, toAuth } = route({ baseURL: 'https://custom.invalid/v1' }, 0)
    await expect(models.getAuth('anthropic')).rejects.toThrow(/OAuth.*native.*route/)
    expect(refresh).not.toHaveBeenCalled()
    expect(toAuth).not.toHaveBeenCalled()
  })

  it('rejects a changed route through the actual adapter stream before transport receives a token', async () => {
    const profile = { baseURL: 'https://custom.invalid/v1' }
    const { auth, toAuth } = route(profile)
    const transport = vi.spyOn(catalogProvider('anthropic')!, 'streamSimple').mockImplementation(() => {
      throw new Error('request reached transport')
    })
    const adapter = new PiAiAdapter({
      profiles: () => resolveProfiles({ anthropic: profile }),
      resolveApiKey: () => Promise.resolve(undefined), auth,
    })
    const consume = async () => {
      for await (const chunk of adapter.stream({
        provider: 'anthropic', model: catalogProvider('anthropic')!.getModels()[0]!.id, messages: [],
      })) { if (chunk.type === 'finish') return chunk }
      return undefined
    }
    await expect(consume()).resolves.toMatchObject({
      type: 'finish', reason: { kind: 'error', failure: { message: expect.stringMatching(/OAuth.*native.*route/) as string } },
    })
    expect(transport).not.toHaveBeenCalled()
    expect(toAuth).not.toHaveBeenCalled()
  })

  it('retains native OAuth for a narrowed catalog with harmless capacity overrides', async () => {
    const native = catalogProvider('anthropic')!.getModels()[0]!
    const { models, toAuth } = route({ models: [{ id: native.id, contextWindow: 65_536 }] })
    await expect(models.getAuth('anthropic')).resolves.toMatchObject({ auth: { apiKey: 'subscription-token' } })
    expect(toAuth).toHaveBeenCalledOnce()
  })

  it('keeps native OAuth refresh on the shared credential record', async () => {
    const { models, auth, refresh, toAuth } = route({}, 0)
    const rotated = { type: 'oauth' as const, access: 'rotated', refresh: 'rotated-refresh', expires: Date.now() + 3_600_000 }
    refresh.mockResolvedValue(rotated)
    await expect(models.getAuth('anthropic')).resolves.toMatchObject({ auth: { apiKey: 'subscription-token' } })
    expect(auth.stored.get('anthropic')).toEqual(rotated)
    expect(refresh).toHaveBeenCalledOnce()
    expect(toAuth).toHaveBeenCalledWith(rotated)
  })

  it('allows an explicit API key on a custom route despite a stored OAuth grant', async () => {
    const { models, refresh, toAuth } = route({
      apiKeyEnv: 'EXPLICIT_KEY', api: 'openai-completions', baseURL: 'https://custom.invalid/v1',
      headers: { 'X-Tenant': 'one' },
    }, 0)
    await expect(models.getAuth('anthropic', { apiKey: 'explicit-key' }))
      .resolves.toMatchObject({ auth: { apiKey: 'explicit-key' } })
    expect(refresh).not.toHaveBeenCalled()
    expect(toAuth).not.toHaveBeenCalled()
  })
})
