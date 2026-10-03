/** Account operations bound only while the Host advertises its modelAccess namespace. */
import type { Context } from '@deepseek-ai/cordis'
import type {
  ModelAccessAttemptOwner, ModelAccessAttemptStatus, ModelAccessConfiguration,
  ModelAccessProviderRequest, ModelAccessRespondRequest, ModelAccessSaveRequest,
  ModelAccessStartRequest, ModelAccessVerifyRequest, ModelAccessVerifyValue,
} from '@deepseek-ai/dsh-api-remotes/client'

/** A public account result contains no secret or supplier diagnostic text. */
export type ModelAccessResult<T> = { ok: true; value: T } | { ok: false; errorCode: string; configurationSaved?: true }
/** Callbacks supplied by the account slot's declared Remote dependency. */
export interface ModelAccessOperations {
  /** Read installed login methods and independent credential/configuration facts. */
  configuration(this: void): Promise<ModelAccessResult<ModelAccessConfiguration>>
  /** Begin one installed authorization method. */
  start(this: void, request: ModelAccessStartRequest): Promise<ModelAccessResult<ModelAccessAttemptOwner>>
  /** Read only an attempt owned by this page. */
  status(this: void, request: ModelAccessAttemptOwner): Promise<ModelAccessResult<ModelAccessAttemptStatus>>
  /** Answer the current prompt without retaining its value. */
  respond(this: void, request: ModelAccessRespondRequest): Promise<ModelAccessResult<ModelAccessAttemptStatus>>
  /** Cancel a page-owned attempt. */
  cancel(this: void, request: ModelAccessAttemptOwner): Promise<ModelAccessResult<ModelAccessAttemptStatus>>
  /** Explicitly select the provider's authentication mode. */
  save(this: void, request: ModelAccessSaveRequest): Promise<ModelAccessResult<ModelAccessConfiguration>>
  /** Delete the Host credential record; supplier-side grants remain separate. */
  logout(this: void, request: ModelAccessProviderRequest): Promise<ModelAccessResult<ModelAccessConfiguration>>
  /** Send the Host's fixed small request after explicit user confirmation. */
  verify(this: void, request: ModelAccessVerifyRequest): Promise<ModelAccessResult<ModelAccessVerifyValue>>
  /** Read the current provider catalog without invoking a model. */
  models(this: void, provider: string): Promise<ModelAccessResult<readonly { id: string; name?: string }[]>>
}

/**
 * Bind plain callbacks within the optional modelAccess service scope.
 * @param ctx - scope declaring remote.modelAccess.
 * @returns callbacks with sanitized failures, including namespace withdrawal.
 */
export function createModelAccessOperations(ctx: Context): ModelAccessOperations {
  async function call<T>(
    operation: () => Promise<{ ok: true; value: T } | { ok: false; error: { code: string; details?: unknown } }>,
  ): Promise<ModelAccessResult<T>> {
    try {
      const response = await operation()
      if (response.ok) return response
      const details = response.error.details
      const partial = typeof details === 'object' && details !== null
        && 'reason' in details && details.reason === 'configuration-saved-credential-failed'
      return { ok: false, errorCode: response.error.code, ...partial ? { configurationSaved: true as const } : {} }
    } catch {
      // A retired namespace or lost connection has no supplier result to expose.
      return { ok: false, errorCode: 'unavailable' }
    }
  }
  return {
    configuration: () => call(() => ctx.remote.modelAccess.configuration()),
    start: request => call(() => ctx.remote.modelAccess.start(request)),
    status: request => call(() => ctx.remote.modelAccess.status(request)),
    respond: request => call(() => ctx.remote.modelAccess.respond(request)),
    cancel: request => call(() => ctx.remote.modelAccess.cancel(request)),
    save: request => call(() => ctx.remote.modelAccess.save(request)),
    logout: request => call(() => ctx.remote.modelAccess.logout(request)),
    verify: request => call(() => ctx.remote.modelAccess.verify(request)),
    models: provider => call(() => ctx.remote.modelAccess.discover({ provider })),
  }
}
