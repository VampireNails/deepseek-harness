/** Host-owned model accounts, private authorization attempts, and connection checks. */
import { randomBytes } from 'node:crypto'
import { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
import { brandString } from '@deepseek-ai/dsh-brand'
import { credentialKey, credentialRef, isCredentialKeySegment } from '@deepseek-ai/dsh-credentials'
import type { AuthorizationPrompt } from '@deepseek-ai/dsh-authorization'
import type { SettingsDescriptor } from '@deepseek-ai/dsh-settings'
import { supportedProtocols } from '@deepseek-ai/dsh-llm-pi-ai'
import { Remote, RemoteError, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import { z } from 'zod'
import type {
  ModelAccessAttemptId, ModelAccessOwnerToken, ModelAccessPromptId, ModelAccessPrompt,
  ModelAccessAttemptOwner, ModelAccessAttemptStatus, ModelAccessConfiguration, ModelAccessProfile, ModelAccessProvider,
  ModelAccessSaveRequest, ModelAccessStartRequest, ModelAccessRespondRequest,
  ModelAccessRemoveRequest, ModelAccessProviderRequest, ModelAccessVerifyRequest, ModelAccessVerifyValue,
  ModelAccessDiscoverRequest,
} from './model-access-types.ts'

const providerId = z.string().regex(/^[a-z][a-z0-9-]*$/).max(100)
const ownerSchema = z.object({ attemptId: z.string().max(100), ownerToken: z.string().max(100) }).strict()
const providerSchema = z.object({ provider: providerId }).strict()
const modelSchema = z.object({ id: z.string().min(1).max(300), name: z.string().max(300).optional() }).strict()
const baseURLSchema = z.url().max(2048).refine(value => ['http:', 'https:'].includes(new URL(value).protocol))
const saveSchema = z.object({
  provider: providerId, expectedRevision: z.number().int().nonnegative(), authentication: z.enum(['api-key', 'oauth', 'ambient']),
  apiKey: z.string().min(1).max(16384).regex(/^[\x20-\x7e]+$/).optional(),
  baseURL: baseURLSchema.optional(), api: z.string().min(1).max(100).optional(),
  displayName: z.string().max(300).optional(), models: z.array(modelSchema).min(1).max(256).optional(),
}).strict()

/** Authorization retention and verification bounds, configured on this plugin. */
export interface ModelAccessOptions {
  /** Maximum time for one login conversation, in milliseconds. */
  attemptTtlMs?: number
  /** Time completed conversations remain available to their owner, in milliseconds. */
  retentionMs?: number
  /** Maximum combined running and retained conversations. */
  maxAttempts?: number
  /** Connection check lifetime, in milliseconds. */
  verificationTimeoutMs?: number
}

interface PendingQuestion {
  view: ModelAccessPrompt
  resolve(value: string): void
  reject(error: Error): void
  cleanup(): void
}
interface Attempt {
  owner: ModelAccessAttemptOwner
  state: ModelAccessAttemptStatus
  controller: AbortController
  question?: PendingQuestion
  timer: ReturnType<typeof setTimeout>
  expiredAt?: number
}

declare module '@deepseek-ai/cordis' {
  interface Context { modelAccessController: ModelAccessController }
}
declare module '@deepseek-ai/dsh-typert-protocol' {
  interface RemoteErrorDetailsMap { 'model-access/rejected': { readonly reason: string } }
}

class ModelAccessRejection extends RemoteError {
  constructor(reason: string) { super('model-access/rejected', `Model access: ${reason}`, { reason }) }
}
function reject(reason: string): never {
  throw new ModelAccessRejection(reason)
}
function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  const result = schema.safeParse(value)
  if (!result.success) reject('invalid-request')
  return result.data
}
function object(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function profileOf(descriptor: SettingsDescriptor, provider: string): Record<string, unknown> {
  return object(object(object(descriptor.value).providers)[provider])
}
function profileView(raw: Record<string, unknown>): ModelAccessProfile {
  const view: ModelAccessProfile = {}
  for (const key of ['apiKeyEnv', 'displayName', 'api', 'baseURL'] as const) {
    if (typeof raw[key] === 'string') view[key] = raw[key]
  }
  if (Array.isArray(raw.models)) view.models = raw.models.flatMap((value) => {
    const model = object(value)
    return typeof model.id !== 'string' ? [] : [{ id: model.id, ...typeof model.name === 'string' ? { name: model.name } : {} }]
  })
  return view
}
function managedRef(provider: string): string { return `DSH_MODEL_${provider.toUpperCase().replaceAll('-', '_')}_API_KEY` }
function requestError(error: unknown): never {
  if (error instanceof ModelAccessRejection) throw error
  if (typeof error === 'object' && error !== null && Reflect.get(error, 'code') === 'SETTINGS_CONFLICT') reject('conflict')
  reject('operation-failed')
}

/** Model account Remote namespace; authentication answers and tokens remain on Host. */
export class ModelAccessController extends TypertRemoteService {
  static Config = Schema.object({
    attemptTtlMs: Schema.number().min(1000).max(3_600_000).default(600_000),
    retentionMs: Schema.number().min(1000).max(3_600_000).default(300_000),
    maxAttempts: Schema.number().step(1).min(1).max(256).default(32),
    verificationTimeoutMs: Schema.number().min(1000).max(120_000).default(30_000),
  })
  private readonly options: Required<ModelAccessOptions>
  private readonly attempts = new Map<ModelAccessAttemptId, Attempt>()
  private readonly writes = new Map<string, Promise<unknown>>()
  private disposed = false

  /**
   * Register bounded model-account operations on the Host.
   * @param ctx - Host services.
   * @param options - bounded lifetime settings.
   */
  constructor(ctx: Context, options: ModelAccessOptions = {}) {
    super(ctx, 'modelAccessController', { namespace: 'modelAccess' })
    this.options = ModelAccessController.Config(options)
    ctx.effect(() => () => {
      this.disposed = true
      for (const attempt of this.attempts.values()) {
        clearTimeout(attempt.timer)
        attempt.controller.abort()
        this.retireQuestion(attempt)
      }
      this.attempts.clear()
    })
  }

  /**
   * Read installed provider profiles and credential metadata.
   * @returns provider profiles and metadata, including installed dormant routes.
   */
  @Remote
  async configuration(): Promise<ModelAccessConfiguration> {
    try {
      const settings = this.settings()
      const llm = this.llm()
      const descriptors = settings.describe({ redactSecrets: true })
      const entries = llm.listConfigurableProviders()
      const credentials = this.ctx.get('credentials')
      const authorization = this.ctx.get('authorization')
      const providers = await Promise.all(entries.map(async (entry): Promise<ModelAccessProvider> => {
        const descriptor = descriptors.find(item => item.ns === entry.settingsNs)
        const raw = descriptor === undefined ? {} : profileOf(descriptor, entry.provider)
        const profile = profileView(raw)
        const editable = entry.credentialScope === 'llm-pi-ai' && entry.settingsPath.length === 2 && entry.settingsPath[0] === 'providers' && isCredentialKeySegment(entry.provider)
        const key = editable ? credentialKey('llm-pi-ai', entry.provider) : undefined
        const record = key === undefined || credentials === undefined
          ? { configured: false, writable: false }
          : await credentials.describeRecord(key)
        const credential = profile.apiKeyEnv === undefined || credentials === undefined
          ? undefined
          : await credentials.describe(credentialRef(profile.apiKeyEnv))
        const flow = key === undefined ? undefined : authorization?.describe(key)
        return {
          id: entry.provider, name: entry.displayName, settingsNs: entry.settingsNs,
          revision: descriptor?.revision ?? 0, configured: llm.listProviders().some(row => row.id === entry.provider),
          declared: entry.declared ?? false, editable,
          authentication: (profile.apiKeyEnv !== undefined || record.kind === 'api-key' ? 'api-key' : record.kind === 'grant' ? 'oauth' : 'ambient'),
          profile, methods: (flow?.methods ?? []).map(method => ({ id: method.id, label: method.label })),
          inFlight: flow?.inFlight ?? false,
          ...credential === undefined || profile.apiKeyEnv === undefined ? {} : { credential: {
            ref: profile.apiKeyEnv, configured: credential.configured, writable: credential.writable,
            managed: profile.apiKeyEnv === managedRef(entry.provider),
            ...credential.source === undefined ? {} : { source: credential.source },
          } },
          record: { configured: record.configured, writable: record.writable, ...record.kind === undefined ? {} : { kind: record.kind } },
        }
      }))
      const customNs = entries.find(entry => entry.credentialScope === 'llm-pi-ai' && entry.settingsPath[0] === 'providers')?.settingsNs
      const descriptor = descriptors.find(item => item.ns === customNs)
      return {
        writable: settings.writable, providers, apis: [...supportedProtocols()],
        ...descriptor === undefined ? {} : { custom: { settingsNs: String(descriptor.ns), revision: descriptor.revision } },
      }
    } catch (error) { return requestError(error) }
  }

  /**
   * Commit an explicit authentication mode and provider profile.
   * @param request - revision and explicit authentication mode.
   * @returns committed public configuration.
   */
  @Remote
  async save(request: ModelAccessSaveRequest): Promise<ModelAccessConfiguration> {
    const parsed = parse(saveSchema, request)
    const ns = await this.namespaceFor(parsed.provider)
    return this.write(ns, async () => {
      const descriptor = this.descriptor(ns, parsed.expectedRevision)
      const current = profileOf(descriptor, parsed.provider)
      const candidate: Record<string, unknown> = { ...current }
      const key = credentialKey('llm-pi-ai', parsed.provider)
      if (this.ctx.get('authorization')?.describe(key)?.inFlight) reject('in-flight')
      const credentials = this.credentials()
      const record = await credentials.describeRecord(key)
      if (parsed.authentication === 'oauth') {
        const flow = this.ctx.get('authorization')?.describe(key)
        if (!flow?.methods.some(method => method.id === 'oauth') || record.kind !== 'grant') reject('subscription-not-authorized')
        if (parsed.baseURL !== undefined || parsed.api !== undefined || parsed.apiKey !== undefined) reject('subscription-custom-endpoint')
        const base = object(object(object(descriptor.base).providers)[parsed.provider])
        if (['apiKeyEnv', 'baseURL', 'api', 'headers'].some(field => base[field] !== undefined)) reject('subscription-base-override')
        delete candidate.apiKeyEnv
        delete candidate.baseURL
        delete candidate.api
        delete candidate.headers
        delete candidate.modelOverrides
        // Curated models carry only id/name; inherited per-model headers/endpoints are excluded.
        if (Array.isArray(current.models)) candidate.models = profileView(current).models ?? []
      } else if (parsed.authentication === 'api-key') {
        if (parsed.apiKey !== undefined) candidate.apiKeyEnv = managedRef(parsed.provider)
        else if (typeof current.apiKeyEnv === 'string' && (await credentials.describe(credentialRef(current.apiKeyEnv))).configured) {
          candidate.apiKeyEnv = current.apiKeyEnv
        }
        else if (record.kind === 'api-key') delete candidate.apiKeyEnv
        else if ((await credentials.describe(credentialRef(managedRef(parsed.provider)))).configured) {
          candidate.apiKeyEnv = managedRef(parsed.provider)
        }
        else reject('api-key-required')
      } else {
        if (record.configured) reject('credential-record-present')
        if (parsed.apiKey !== undefined) reject('invalid-request')
        delete candidate.apiKeyEnv
        const base = object(object(object(descriptor.base).providers)[parsed.provider])
        if (base.apiKeyEnv !== undefined) reject('ambient-base-override')
      }
      for (const field of ['baseURL', 'api', 'displayName', 'models'] as const) if (parsed[field] !== undefined) candidate[field] = parsed[field]
      if (parsed.api !== undefined && !supportedProtocols().includes(parsed.api)) reject('unsupported-api')
      if ((await this.configuration()).providers.find(row => row.id === parsed.provider)?.declared !== false
        && (typeof candidate.api !== 'string' || typeof candidate.baseURL !== 'string' || !Array.isArray(candidate.models) || candidate.models.length === 0)) reject('custom-profile-incomplete')
      // Commit configuration first: stale/invalid writes cannot overwrite a working credential.
      await this.settings().mutate(ns, [{ op: 'set', path: ['providers', parsed.provider], value: candidate }], parsed.expectedRevision)
      if (parsed.apiKey !== undefined) {
        try { await credentials.set(credentialRef(managedRef(parsed.provider)), parsed.apiKey) }
        catch { reject('configuration-saved-credential-failed') }
      }
      return this.configuration()
    })
  }

  /**
   * Remove a provider from the writable configuration layer.
   * @param request - revision-checked removal; credentials are retained.
   * @returns current configuration.
   */
  @Remote
  async removeProvider(request: ModelAccessRemoveRequest): Promise<ModelAccessConfiguration> {
    const parsed = parse(z.object({ provider: providerId, expectedRevision: z.number().int().nonnegative() }).strict(), request)
    const ns = await this.namespaceFor(parsed.provider)
    return this.write(ns, async () => {
      const descriptor = this.descriptor(ns, parsed.expectedRevision)
      if (object(object(descriptor.base).providers)[parsed.provider] !== undefined) reject('base-provider-retained')
      await this.settings().mutate(ns, [{ op: 'unset', path: ['providers', parsed.provider] }], parsed.expectedRevision)
      return this.configuration()
    })
  }

  /**
   * Delete page-managed API keys while preserving other credential sources.
   * @param request - provider whose page-managed key is cleared.
   * @returns remaining effective sources.
   */
  @Remote
  async clearKey(request: ModelAccessProviderRequest): Promise<ModelAccessConfiguration> {
    const parsed = parse(providerSchema, request)
    await this.namespaceFor(parsed.provider)
    return this.write(parsed.provider, async () => {
      if (this.ctx.get('authorization')?.describe(credentialKey('llm-pi-ai', parsed.provider))?.inFlight) reject('in-flight')
      await this.credentials().unset(credentialRef(managedRef(parsed.provider)))
      const key = credentialKey('llm-pi-ai', parsed.provider)
      if ((await this.credentials().describeRecord(key)).kind === 'api-key') await this.credentials().deleteRecord(key)
      return this.configuration()
    })
  }

  /**
   * Delete the stored subscription grant without changing other credential sources.
   * @param request - provider whose stored subscription grant is deleted.
   * @returns remaining effective sources.
   */
  @Remote
  async logout(request: ModelAccessProviderRequest): Promise<ModelAccessConfiguration> {
    const parsed = parse(providerSchema, request)
    return this.write(parsed.provider, async () => {
      const key = credentialKey('llm-pi-ai', parsed.provider)
      if (this.ctx.get('authorization')?.describe(key)?.inFlight) reject('in-flight')
      if ((await this.credentials().describeRecord(key)).kind === 'grant') await this.credentials().deleteRecord(key)
      return this.configuration()
    })
  }

  /**
   * Start a private authorization attempt with bounded retention.
   * @param request - provider and its installed method.
   * @returns a private owner capability immediately.
   */
  @Remote
  start(request: ModelAccessStartRequest): ModelAccessAttemptOwner {
    const parsed = parse(z.object({ provider: providerId, method: z.string().min(1).max(100) }).strict(), request)
    const authorization = this.ctx.get('authorization')
    if (authorization === undefined) reject('authorization-unavailable')
    const key = credentialKey('llm-pi-ai', parsed.provider)
    const flow = authorization.describe(key)
    if (!flow?.methods.some(method => method.id === parsed.method)) reject('method-unavailable')
    if (flow.inFlight || [...this.attempts.values()].some(attempt => attempt.state.provider === parsed.provider && attempt.state.status === 'pending')) reject('in-flight')
    if (this.writes.size > 0) reject('configuration-busy')
    if (this.attempts.size >= this.options.maxAttempts) reject('attempt-limit')
    const owner = { attemptId: brandString<ModelAccessAttemptId>(randomBytes(24).toString('hex')), ownerToken: brandString<ModelAccessOwnerToken>(randomBytes(32).toString('hex')) }
    const controller = new AbortController()
    const attempt: Attempt = {
      owner, controller,
      state: {
        attemptId: owner.attemptId, provider: parsed.provider, status: 'pending', expiresAt: Date.now() + this.options.attemptTtlMs,
      },
      timer: setTimeout(() => { controller.abort() }, this.options.attemptTtlMs),
    }
    attempt.timer.unref()
    this.attempts.set(owner.attemptId, attempt)
    void authorization.begin({ key, method: parsed.method, signal: controller.signal, interaction: {
      notify: (notice) => {
        if (attempt.state.status !== 'pending') return
        const previous = notice.url === undefined && notice.code === undefined ? attempt.state.notice : undefined
        const url = notice.url ?? previous?.url
        const code = notice.code ?? previous?.code
        attempt.state.notice = {
          message: notice.message.slice(0, 4096),
          ...url === undefined ? {} : { url: url.slice(0, 4096) },
          ...code === undefined ? {} : { code: code.slice(0, 256) },
        }
      },
      prompt: prompt => this.prompt(attempt, prompt),
    } }).then((outcome) => { attempt.state.status = outcome.status }, () => {
      // The authorization seam alone decides cancellation. An admitted commit can fail after the caller withdraws.
      attempt.state.status = 'failed'
      attempt.state.errorCode = 'authorization-failed'
    }).finally(() => {
      this.retireQuestion(attempt)
      clearTimeout(attempt.timer)
      if (this.disposed) return
      attempt.expiredAt = Date.now() + this.options.retentionMs
      attempt.timer = setTimeout(() => { this.attempts.delete(owner.attemptId) }, this.options.retentionMs)
      attempt.timer.unref()
    })
    return { ...owner }
  }

  /**
   * Read the current state of an owned authorization attempt.
   * @param request - private attempt capability.
   * @returns latest prompt/notice and terminal result.
   */
  @Remote
  status(request: ModelAccessAttemptOwner): ModelAccessAttemptStatus { return structuredClone(this.owned(request).state) }

  /**
   * Consume one response to the currently owned prompt.
   * @param request - one answer to the current prompt.
   * @returns state after consuming the answer.
   */
  @Remote
  respond(request: ModelAccessRespondRequest): ModelAccessAttemptStatus {
    const parsed = parse(ownerSchema.extend({ promptId: z.string().max(100), value: z.string().max(16384) }).strict(), request)
    const attempt = this.owned({ attemptId: request.attemptId, ownerToken: request.ownerToken })
    const question = attempt.question
    if (question === undefined || question.view.promptId !== parsed.promptId || attempt.state.status !== 'pending') reject('stale-prompt')
    if (question.view.kind === 'select' && !question.view.options.some(option => option.id === parsed.value)) reject('invalid-answer')
    question.cleanup()
    delete attempt.question
    delete attempt.state.prompt
    question.resolve(parsed.value)
    return structuredClone(attempt.state)
  }

  /**
   * Request withdrawal and report the current settlement state.
   * @param request - private attempt capability.
   * @returns state while withdrawal settles.
   */
  @Remote
  cancel(request: ModelAccessAttemptOwner): ModelAccessAttemptStatus {
    const attempt = this.owned(request)
    attempt.controller.abort()
    this.retireQuestion(attempt)
    return structuredClone(attempt.state)
  }

  /**
   * Run the fixed small request through the active model adapter.
   * @param request - registered provider/model.
   * @param signal - caller lifetime.
   * @returns actual fixed-request outcome.
   */
  @Remote
  async verify(request: ModelAccessVerifyRequest, signal: AbortSignal): Promise<ModelAccessVerifyValue> {
    const parsed = parse(z.object({ provider: providerId, model: z.string().min(1).max(300) }).strict(), request)
    const lifetime = AbortSignal.any([signal, AbortSignal.timeout(this.options.verificationTimeoutMs)])
    try {
      for await (const chunk of this.llm().stream({ ...parsed, messages: [{ role: 'user', content: [{ type: 'text', text: 'Reply OK.' }] }], maxTokens: 16, signal: lifetime })) {
        if (chunk.type === 'finish') return chunk.reason.kind === 'stop' || chunk.reason.kind === 'max-tokens' ? { ok: true } : { ok: false, errorCode: lifetime.aborted ? 'cancelled' : 'verification-failed' }
      }
      return { ok: false, errorCode: 'verification-failed' }
    } catch { return { ok: false, errorCode: lifetime.aborted ? 'cancelled' : 'verification-failed' } }
  }

  /**
   * Discover model names through the installed Host adapter.
   * @param request - draft endpoint inputs.
   * @param signal - caller lifetime.
   * @returns discovered model names.
   */
  @Remote
  async discover(request: ModelAccessDiscoverRequest, signal: AbortSignal): Promise<{ id: string; name?: string }[]> {
    const parsed = parse(z.object({
      provider: providerId.optional(), baseURL: baseURLSchema.optional(),
      apiKey: z.string().max(16384).optional(), api: z.string().max(100).optional(),
    }).strict(), request)
    try {
      const ns = await this.namespaceFor(parsed.provider)
      const result = await this.llm().discoverModels(ns, {
        ...parsed.provider === undefined ? {} : { provider: parsed.provider },
        ...parsed.baseURL === undefined ? {} : { baseURL: parsed.baseURL },
        ...parsed.api === undefined ? {} : { api: parsed.api },
        ...parsed.apiKey === undefined ? {} : { apiKey: parsed.apiKey },
      }, signal)
      return result.map(model => ({ id: model.id, ...model.name === undefined ? {} : { name: model.name } }))
    } catch (error) { return requestError(error) }
  }

  private prompt(attempt: Attempt, prompt: AuthorizationPrompt): Promise<string> {
    this.retireQuestion(attempt)
    if (attempt.controller.signal.aborted || prompt.signal?.aborted) return Promise.reject(new Error('prompt withdrawn'))
    const promptId = brandString<ModelAccessPromptId>(randomBytes(16).toString('hex'))
    const common = { promptId, message: prompt.message.slice(0, 4096) }
    const view: ModelAccessPrompt = prompt.kind === 'select'
      ? { ...common, kind: 'select', options: prompt.options.slice(0, 64).map(option => ({ id: option.id.slice(0, 256), label: option.label.slice(0, 256), ...option.description === undefined ? {} : { description: option.description.slice(0, 1024) } })) }
      : { ...common, kind: prompt.kind, ...prompt.placeholder === undefined ? {} : { placeholder: prompt.placeholder.slice(0, 1024) } }
    return new Promise<string>((resolve, rejectPrompt) => {
      const withdraw = () => { if (attempt.question?.view.promptId === promptId) this.retireQuestion(attempt) }
      const cleanup = () => { prompt.signal?.removeEventListener('abort', withdraw); attempt.controller.signal.removeEventListener('abort', withdraw) }
      attempt.question = { view, resolve, reject: rejectPrompt, cleanup }
      attempt.state.prompt = view
      prompt.signal?.addEventListener('abort', withdraw, { once: true })
      attempt.controller.signal.addEventListener('abort', withdraw, { once: true })
    })
  }
  private retireQuestion(attempt: Attempt): void {
    const question = attempt.question
    delete attempt.question
    delete attempt.state.prompt
    question?.cleanup()
    question?.reject(new Error('prompt withdrawn'))
  }
  private owned(owner: ModelAccessAttemptOwner): Attempt {
    const parsed = parse(ownerSchema, owner)
    const attempt = this.attempts.get(owner.attemptId)
    if (attempt === undefined || attempt.owner.ownerToken !== parsed.ownerToken || (attempt.expiredAt !== undefined && Date.now() >= attempt.expiredAt)) reject('attempt-not-found')
    return attempt
  }
  private async namespaceFor(provider?: string): Promise<string> {
    const value = await this.configuration()
    const row = value.providers.find(item => item.id === provider)
    if (row !== undefined && !row.editable) reject('provider-not-editable')
    const ns = row?.settingsNs ?? value.custom?.settingsNs
    if (ns === undefined) reject('configuration-unavailable')
    return ns
  }
  private descriptor(ns: string, revision: number): SettingsDescriptor {
    const descriptor = this.settings().describe({ redactSecrets: true }).find(item => item.ns === ns)
    if (descriptor === undefined) reject('configuration-unavailable')
    if (descriptor.revision !== revision) reject('conflict')
    return descriptor
  }
  private async write<T>(_key: string, action: () => Promise<T>): Promise<T> {
    // Credential actions and profile edits share a commit queue, including different providers in one revisioned namespace.
    const key = 'mutations'
    const previous = this.writes.get(key)
    const operation = (previous ?? Promise.resolve()).catch(() => {}).then(action)
    this.writes.set(key, operation)
    try { return await operation }
    catch (error) { return requestError(error) }
    finally { if (this.writes.get(key) === operation) this.writes.delete(key) }
  }
  private settings() { const service = this.ctx.get('settings'); if (service === undefined) reject('settings-unavailable'); return service }
  private credentials() { const service = this.ctx.get('credentials'); if (service === undefined) reject('credentials-unavailable'); return service }
  private llm() { const service = this.ctx.get('llm'); if (service === undefined) reject('llm-unavailable'); return service }
}
