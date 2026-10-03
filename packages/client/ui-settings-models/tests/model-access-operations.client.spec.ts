/** Account callbacks use their declared namespace and strip supplier failure text. */
import { Context } from '@deepseek-ai/cordis'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TestRemote } from '@deepseek-ai/dsh-client-test-runtime'
import { createModelAccessOperations } from '../src/client/model-access-operations.ts'
const contexts: Context[] = []
afterEach(async () => { for (const context of contexts.splice(0)) await context.fiber.dispose() })

describe('model access callbacks', () => {
  it('sanitizes refused and retired namespace calls', async () => {
    const ctx = new Context()
    contexts.push(ctx)
    const configuration = vi.fn()
    new TestRemote(ctx, { modelAccess: { configuration } })
    const operations = createModelAccessOperations(ctx)
    configuration.mockResolvedValueOnce({ ok: false, error: { code: 'model-access/rejected', message: 'private diagnostic' } })
    expect(await operations.configuration()).toEqual({ ok: false, errorCode: 'model-access/rejected' })
    configuration.mockRejectedValueOnce(new Error('private diagnostic'))
    expect(await operations.configuration()).toEqual({ ok: false, errorCode: 'unavailable' })
    configuration.mockResolvedValueOnce({ ok: false, error: {
      code: 'model-access/rejected', message: 'private diagnostic',
      details: { reason: 'configuration-saved-credential-failed', private: 'never expose' },
    } })
    expect(await operations.configuration()).toEqual({ ok: false, errorCode: 'model-access/rejected', configurationSaved: true })
  })
  it('reads verification choices from the existing model catalog', async () => {
    const ctx = new Context()
    contexts.push(ctx)
    const discover = vi.fn(async () => ({ ok: true, value: [{ id: 'gpt-5', name: 'GPT 5' }] }))
    new TestRemote(ctx, { modelAccess: { discover } })
    expect(await createModelAccessOperations(ctx).models('openai-codex')).toEqual({ ok: true, value: [{ id: 'gpt-5', name: 'GPT 5' }] })
    expect(discover).toHaveBeenCalledWith({ provider: 'openai-codex' })
  })
})
