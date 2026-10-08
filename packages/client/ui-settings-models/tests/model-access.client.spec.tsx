// @vitest-environment jsdom
/** Subscription authorization and independent configuration states on the real Models controls. */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  ModelAccessAttemptOwner, ModelAccessAttemptStatus, ModelAccessConfiguration,
  ModelAccessPromptId,
} from '@deepseek-ai/dsh-api-remotes/client'
import { ModelAccessSection } from '../src/client/ModelAccessSection.tsx'
import type { ModelAccessOperations } from '../src/client/model-access-operations.ts'
import { en } from '../src/client/locales.ts'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
const t = (key: keyof typeof en): string => en[key]
const owner = { attemptId: 'attempt-1', ownerToken: 'private-owner' } as ModelAccessAttemptOwner
const promptId = 'question-1' as ModelAccessPromptId
const configuration: ModelAccessConfiguration = {
  writable: true, apis: [], providers: [{
    id: 'openai-codex', name: 'ChatGPT Codex', settingsNs: 'llm-pi-ai', revision: 4,
    configured: false, declared: false, editable: true, authentication: 'ambient',
    profile: { baseURL: 'https://old.example', apiKeyEnv: 'OLD_KEY' },
    methods: [{ id: 'oauth', label: 'Browser login' }], inFlight: false,
    record: { configured: false, writable: true },
  }],
}
const pending: ModelAccessAttemptStatus = {
  attemptId: owner.attemptId, provider: 'openai-codex', status: 'pending', expiresAt: Date.now() + 60000,
  notice: { message: 'Complete sign-in', url: 'https://auth.example/login', code: 'ABCD-EFGH' },
}
function bench(overrides: Partial<ModelAccessOperations> = {}) {
  const operations: ModelAccessOperations = {
    configuration: vi.fn<ModelAccessOperations['configuration']>(async () => ({ ok: true, value: configuration })),
    start: vi.fn<ModelAccessOperations['start']>(async () => ({ ok: true, value: owner })),
    status: vi.fn<ModelAccessOperations['status']>(async () => ({ ok: true, value: pending })),
    respond: vi.fn<ModelAccessOperations['respond']>(async () => ({ ok: true, value: { ...pending, status: 'authorized' } })),
    cancel: vi.fn<ModelAccessOperations['cancel']>(async () => ({ ok: true, value: { ...pending, status: 'cancelled' } })),
    save: vi.fn<ModelAccessOperations['save']>(async () => ({ ok: true, value: configuration })),
    logout: vi.fn<ModelAccessOperations['logout']>(async () => ({ ok: true, value: configuration })),
    verify: vi.fn<ModelAccessOperations['verify']>(async () => ({ ok: true, value: { ok: true } })),
    models: vi.fn<ModelAccessOperations['models']>(async () => ({ ok: true, value: [{ id: 'gpt-5' }] })),
    ...overrides,
  }
  const updated = vi.fn()
  const result = render(<ModelAccessSection operations={operations} t={t} onUpdated={updated} refreshKey="initial" />)
  return { operations, updated, ...result }
}
async function begin(): Promise<void> {
  fireEvent.click(await screen.findByRole('button', { name: en.accountSignIn }))
  await screen.findByText('ABCD-EFGH')
}
describe('model account configuration', () => {
  it('explains unavailable account access without offering sign-in actions', async () => {
    bench({ configuration: async () => ({ ok: false, errorCode: 'unavailable' }) })
    expect(await screen.findByText(en.accountUnavailable)).toBeDefined()
    expect(screen.queryByRole('button', { name: en.accountSignIn })).toBeNull()
  })
  it('renders only methods supplied by the Host and links device-code authorization', async () => {
    const b = bench()
    await begin()
    expect(screen.getByRole('link', { name: en.accountOpenBrowser }).getAttribute('href')).toBe('https://auth.example/login')
    expect(b.operations.start).toHaveBeenCalledWith({ provider: 'openai-codex', method: 'oauth' })
    expect(b.operations.status).toHaveBeenCalledWith(owner)
    expect(screen.getByText(en.accountDisabled)).toBeDefined()
    expect(screen.getByText(en.accountRecordMissing)).toBeDefined()
  })
  it('keeps API-key-only records in metadata without account login or grant logout actions', async () => {
    bench({ configuration: async () => ({ ok: true, value: { ...configuration, providers: [{
      ...configuration.providers[0]!, configured: true, authentication: 'api-key',
      methods: [{ id: 'api-key', label: 'API key' }],
      record: { configured: true, writable: true, kind: 'api-key' },
    }] } }) })
    await screen.findByText(en.accountRecordSaved)
    expect(screen.queryByRole('button', { name: en.accountSignIn })).toBeNull()
    expect(screen.queryByRole('button', { name: en.accountLogout })).toBeNull()
    expect(screen.queryByRole('button', { name: en.accountEnable })).toBeNull()
    expect(screen.getByRole('button', { name: en.accountVerify })).toBeDefined()
  })
  it('keeps independent account facts in its owner-local rendered-output snapshot', async () => {
    bench({ configuration: async () => ({ ok: true, value: { ...configuration, providers: [{
      ...configuration.providers[0]!, configured: true, authentication: 'api-key',
      credential: { ref: 'OLD_KEY', configured: true, writable: false, managed: false, source: 'environment' },
      record: { configured: true, writable: true, kind: 'grant' },
    }] } }) })
    await screen.findByText(en.accountRecordSaved)
    expect(screen.getByRole('region', { name: en.accountTitle }).textContent).toMatchSnapshot()
  })
  it.each(['text', 'secret'] as const)('answers a %s prompt without retaining the answer', async (kind) => {
    const b = bench({ status: async () => ({ ok: true, value: {
      ...pending, prompt: { promptId, kind, message: 'Paste callback', placeholder: 'Callback value' },
    } }) })
    await begin()
    const input = screen.getByLabelText('Paste callback')
    expect(input.getAttribute('type')).toBe(kind === 'secret' ? 'password' : 'text')
    fireEvent.change(input, { target: { value: 'private-response' } })
    fireEvent.click(screen.getByRole('button', { name: en.accountRespond }))
    await screen.findByText(en.accountAuthorized)
    expect(b.operations.respond).toHaveBeenCalledWith({ ...owner, promptId, value: 'private-response' })
    expect(screen.queryByDisplayValue('private-response')).toBeNull()
    expect(b.operations.save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: en.accountEnable }))
    await waitFor(() => {
      expect(b.operations.save).toHaveBeenCalledWith({
        provider: 'openai-codex', expectedRevision: 4, authentication: 'oauth',
      })
    })
    expect(b.updated).toHaveBeenCalled()
  })
  it('requires an explicit choice for a select prompt', async () => {
    const b = bench({ status: async () => ({ ok: true, value: { ...pending, prompt: {
      promptId, kind: 'select', message: 'Account type', options: [
        { id: 'business', label: 'Business', description: 'Organization account' },
        { id: 'personal', label: 'Personal' },
      ],
    } } }) })
    await begin()
    expect(screen.getByRole('button', { name: en.accountRespond })).toHaveProperty('disabled', true)
    fireEvent.change(screen.getByLabelText('Account type'), { target: { value: 'personal' } })
    fireEvent.click(screen.getByRole('button', { name: en.accountRespond }))
    await screen.findByText(en.accountAuthorized)
    expect(b.operations.respond).toHaveBeenCalledWith({ ...owner, promptId, value: 'personal' })
  })
  it('cancels an attempt and stops polling when its page unmounts', async () => {
    const b = bench()
    await begin()
    b.unmount()
    await waitFor(() => { expect(b.operations.cancel).toHaveBeenCalledWith(owner) })
  })
  it('cancels a late start result after its page unmounts', async () => {
    let finish: ((result: { ok: true; value: ModelAccessAttemptOwner }) => void) | undefined
    const b = bench({ start: () => new Promise((resolve) => { finish = resolve }) })
    fireEvent.click(await screen.findByRole('button', { name: en.accountSignIn }))
    b.unmount()
    finish!({ ok: true, value: owner })
    await waitFor(() => { expect(b.operations.cancel).toHaveBeenCalledWith(owner) })
  })
  it('ends polling at the Host TTL and cancels the pending attempt', async () => {
    vi.useFakeTimers()
    const b = bench({ status: vi.fn<ModelAccessOperations['status']>(async () => ({ ok: true, value: { ...pending, expiresAt: Date.now() + 100 } })) })
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.accountSignIn }))
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(1001) })
    expect(screen.getByText(en.accountExpired)).toBeDefined()
    expect(b.operations.cancel).toHaveBeenCalledWith(owner)
    const reads = vi.mocked(b.operations.status).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(b.operations.status).toHaveBeenCalledTimes(reads)
  })
  it('pauses status requests while the browser page is hidden', async () => {
    vi.useFakeTimers()
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const b = bench()
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.accountSignIn }))
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(b.operations.status).not.toHaveBeenCalled()
    visibility.mockReturnValue('visible')
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(b.operations.status).toHaveBeenCalledWith(owner)
  })
  it('ignores a stale poll arriving after a prompt response settles', async () => {
    vi.useFakeTimers()
    const question: ModelAccessAttemptStatus = { ...pending, prompt: { promptId, kind: 'text', message: 'Paste callback' } }
    let finish: ((result: { ok: true; value: ModelAccessAttemptStatus }) => void) | undefined
    const status = vi.fn()
      .mockResolvedValueOnce({ ok: true, value: question })
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    bench({ status })
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.accountSignIn }))
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(status).toHaveBeenCalledTimes(2)
    fireEvent.change(screen.getByLabelText('Paste callback'), { target: { value: 'callback-result' } })
    fireEvent.click(screen.getByRole('button', { name: en.accountRespond }))
    await act(async () => {})
    expect(screen.getByText(en.accountAuthorized)).toBeDefined()
    await act(async () => { finish!({ ok: true, value: question }) })
    expect(screen.queryByLabelText('Paste callback')).toBeNull()
    expect(screen.getByText(en.accountAuthorized)).toBeDefined()
  })
  it('retains its owner after a status-read failure and resumes on retry', async () => {
    const status = vi.fn().mockResolvedValueOnce({ ok: false, errorCode: 'unavailable' })
      .mockResolvedValue({ ok: true, value: pending })
    const b = bench({ status })
    fireEvent.click(await screen.findByRole('button', { name: en.accountSignIn }))
    await screen.findByText(en.accountTemporaryFailure)
    expect(b.operations.cancel).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: en.retry }))
    await screen.findByText('ABCD-EFGH')
    expect(status.mock.calls.every(([request]) => request === owner)).toBe(true)
    expect(screen.queryByText(en.accountTemporaryFailure)).toBeNull()
  })
  it('handles a start refusal without entering an authorization conversation', async () => {
    const b = bench({ start: async () => ({ ok: false, errorCode: 'model-access/rejected' }) })
    fireEvent.click(await screen.findByRole('button', { name: en.accountSignIn }))
    await screen.findByText(en.accountFailed)
    expect(b.operations.status).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: en.accountSignIn })).toBeDefined()
  })
  it('cancels explicitly and hides secret prompts after settlement', async () => {
    const b = bench({ status: async () => ({ ok: true, value: { ...pending,
      prompt: { promptId, kind: 'secret', message: 'Secret answer' },
    } }) })
    await begin()
    fireEvent.change(screen.getByLabelText('Secret answer'), { target: { value: 'secret-value' } })
    fireEvent.click(screen.getByRole('button', { name: en.cancel }))
    await screen.findByText(en.accountCancelled)
    expect(screen.queryByDisplayValue('secret-value')).toBeNull()
    expect(b.operations.cancel).toHaveBeenCalledWith(owner)
  })
  it('keeps polling an admitted cancellation until the Host reports terminal settlement', async () => {
    vi.useFakeTimers()
    const status = vi.fn().mockResolvedValueOnce({ ok: true, value: pending })
      .mockResolvedValue({ ok: true, value: { ...pending, status: 'authorized' } })
    const b = bench({ status, cancel: vi.fn<ModelAccessOperations['cancel']>(async () => ({ ok: true, value: pending })) })
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.accountSignIn }))
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.cancel }))
    await act(async () => {})
    expect(screen.getByText(en.accountCancelling)).toBeDefined()
    expect(screen.queryByText(en.accountCancelled)).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByText(en.accountAuthorized)).toBeDefined()
    expect(b.operations.status).toHaveBeenCalledWith(owner)
  })
  it('reconciles an admitted credential write across the Host TTL without claiming cancellation', async () => {
    vi.useFakeTimers()
    const expiring = { ...pending, expiresAt: Date.now() + 100 }
    const status = vi.fn().mockResolvedValueOnce({ ok: true, value: expiring })
      .mockResolvedValue({ ok: true, value: { ...expiring, status: 'authorized' } })
    const b = bench({ status, cancel: vi.fn<ModelAccessOperations['cancel']>(async () => ({ ok: true, value: expiring })) })
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.accountSignIn }))
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(1001) })
    expect(b.operations.cancel).toHaveBeenCalledWith(owner)
    expect(screen.getByText(en.accountAuthorized)).toBeDefined()
    expect(screen.queryByText(en.accountExpired)).toBeNull()
    expect(screen.queryByText(en.accountCancelled)).toBeNull()
  })
  it('bounds post-TTL reconciliation and leaves an unconfirmed outcome explicitly unknown', async () => {
    vi.useFakeTimers()
    const expiring = { ...pending, expiresAt: Date.now() + 100 }
    const status = vi.fn<ModelAccessOperations['status']>(async () => ({ ok: true, value: expiring }))
    const b = bench({ status, cancel: vi.fn<ModelAccessOperations['cancel']>(async () => ({ ok: true, value: expiring })) })
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: en.accountSignIn }))
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(1001) })
    expect(screen.getByText(en.accountExpiryPending)).toBeDefined()
    await act(async () => { await vi.advanceTimersByTimeAsync(31000) })
    expect(screen.getByText(en.accountOutcomeUnknown)).toBeDefined()
    expect(screen.queryByText(en.accountCancelled)).toBeNull()
    expect(screen.queryByText(en.accountFailed)).toBeNull()
    const reads = status.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(status).toHaveBeenCalledTimes(reads)
    expect(b.operations.cancel).toHaveBeenCalledWith(owner)
  })
  it('refreshes account metadata when the existing provider editors change their page snapshot', async () => {
    const configurationRead = vi.fn(async () => ({ ok: true as const, value: configuration }))
    const b = bench({ configuration: configurationRead })
    await screen.findByText(en.accountDisabled)
    configurationRead.mockResolvedValue({ ok: true, value: { ...configuration, providers: [{
      ...configuration.providers[0]!, configured: true, authentication: 'api-key',
    }] } })
    b.rerender(<ModelAccessSection operations={b.operations} t={t} onUpdated={b.updated} refreshKey="edited-key" />)
    expect(await screen.findByText(en.accountEnabled)).toBeDefined()
    expect(screen.getByText(en.accountEffectiveSource.replace('{source}', en.accountAuthApiKey))).toBeDefined()
  })
  it('reports a committed configuration separately from its failed credential write', async () => {
    bench({
      status: async () => ({ ok: true, value: { ...pending, status: 'authorized' } }),
      save: async () => ({ ok: false, errorCode: 'model-access/rejected', configurationSaved: true }),
    })
    fireEvent.click(await screen.findByRole('button', { name: en.accountSignIn }))
    await screen.findByText(en.accountAuthorized)
    fireEvent.click(screen.getByRole('button', { name: en.accountEnable }))
    expect(await screen.findByText(en.accountPartialSave)).toBeDefined()
  })
  it('retains a saved account when enabling the provider fails', async () => {
    bench({
      status: async () => ({ ok: true, value: { ...pending, status: 'authorized' } }),
      save: async () => ({ ok: false, errorCode: 'settings/conflict' }),
    })
    fireEvent.click(await screen.findByRole('button', { name: en.accountSignIn }))
    await screen.findByText(en.accountAuthorized)
    fireEvent.click(screen.getByRole('button', { name: en.accountEnable }))
    expect(await screen.findByText(en.accountEnableFailed)).toBeDefined()
    expect(screen.getByRole('button', { name: en.accountEnable })).toBeDefined()
  })
  it('confirms Host record deletion and explains supplier-side revocation', async () => {
    const enabled: ModelAccessConfiguration = { ...configuration, providers: [{
      ...configuration.providers[0]!, configured: true, authentication: 'oauth',
      record: { configured: true, writable: true, kind: 'grant' },
    }] }
    const b = bench({ configuration: async () => ({ ok: true, value: enabled }) })
    fireEvent.click(await screen.findByRole('button', { name: en.accountLogout }))
    expect(await screen.findByText(en.accountLogoutDescription)).toBeDefined()
    expect(b.operations.logout).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: en.accountLogoutConfirm }))
    await waitFor(() => { expect(b.operations.logout).toHaveBeenCalledWith({ provider: 'openai-codex' }) })
  })
  it('requires user confirmation before a fixed-cost model request', async () => {
    const enabled: ModelAccessConfiguration = { ...configuration, providers: [{ ...configuration.providers[0]!, configured: true }] }
    const b = bench({ configuration: async () => ({ ok: true, value: enabled }) })
    fireEvent.click(await screen.findByRole('button', { name: en.accountVerify }))
    expect(await screen.findByText(en.accountVerifyDescription)).toBeDefined()
    await screen.findByRole('option', { name: 'gpt-5' })
    expect(b.operations.verify).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: en.accountVerifyConfirm }))
    expect(await screen.findByText(en.accountVerified)).toBeDefined()
    expect(b.operations.verify).toHaveBeenCalledWith({ provider: 'openai-codex', model: 'gpt-5' })
  })
})
