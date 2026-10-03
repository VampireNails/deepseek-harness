/** Wire-safe model account configuration and authorization polling contracts. */
import type { Branded } from '@deepseek-ai/dsh-brand'
import type { CredentialInfo } from '@deepseek-ai/dsh-credentials/types'
import type { AuthorizationMethod, AuthorizationNotice } from '@deepseek-ai/dsh-authorization/types'

/** Host-generated identity of one authorization conversation. */
export type ModelAccessAttemptId = Branded<'ModelAccessAttemptId'>
/** Capability required to observe or change an authorization conversation. */
export type ModelAccessOwnerToken = Branded<'ModelAccessOwnerToken'>
/** Host-generated identity of the currently pending question. */
export type ModelAccessPromptId = Branded<'ModelAccessPromptId'>
/** Curated, non-secret editable provider profile. */
export interface ModelAccessProfile {
  apiKeyEnv?: string
  displayName?: string
  api?: string
  baseURL?: string
  models?: { id: string; name?: string }[]
}
/** Public provider configuration; credentials contain metadata only. */
export interface ModelAccessProvider {
  id: string
  name: string
  settingsNs: string
  revision: number
  configured: boolean
  declared: boolean
  editable: boolean
  /** Selected configuration mode; presence and verification are separate facts. */
  authentication: 'api-key' | 'oauth' | 'ambient'
  profile: ModelAccessProfile
  methods: AuthorizationMethod[]
  inFlight: boolean
  credential?: CredentialInfo & { ref: string; managed: boolean }
  record: { configured: boolean; writable: boolean; kind?: 'api-key' | 'grant' }
}
/** Configuration returned to the model accounts page. */
export interface ModelAccessConfiguration {
  writable: boolean
  providers: ModelAccessProvider[]
  custom?: { settingsNs: string; revision: number }
  apis: string[]
}
/** Explicit save mode prevents subscription tokens reaching custom endpoints. */
export interface ModelAccessSaveRequest {
  provider: string
  expectedRevision: number
  authentication: 'api-key' | 'oauth' | 'ambient'
  apiKey?: string
  baseURL?: string
  api?: string
  displayName?: string
  models?: { id: string; name?: string }[]
}
/** Revision-checked provider removal. Credentials are retained. */
export interface ModelAccessRemoveRequest { provider: string; expectedRevision: number }
/** Credential action addressed to one provider. */
export interface ModelAccessProviderRequest { provider: string }
/** Authorization request naming one installed login method. */
export interface ModelAccessStartRequest { provider: string; method: string }
/** Private handle returned only to the initiating page. */
export interface ModelAccessAttemptOwner { attemptId: ModelAccessAttemptId; ownerToken: ModelAccessOwnerToken }
/** Wire-safe pending prompt; its signal remains within the Host. */
export type ModelAccessPrompt = { promptId: ModelAccessPromptId; message: string } & (
  { kind: 'text' | 'secret'; placeholder?: string }
  | { kind: 'select'; options: { id: string; label: string; description?: string }[] }
)
/** Current bounded authorization state; no answer or token is ever returned. */
export interface ModelAccessAttemptStatus {
  attemptId: ModelAccessAttemptId
  provider: string
  status: 'pending' | 'authorized' | 'cancelled' | 'failed'
  expiresAt: number
  notice?: AuthorizationNotice
  prompt?: ModelAccessPrompt
  errorCode?: string
}
/** Answer to the current question, consumed without being retained. */
export interface ModelAccessRespondRequest extends ModelAccessAttemptOwner { promptId: ModelAccessPromptId; value: string }
/** Explicit fixed-cost connection check. */
export interface ModelAccessVerifyRequest { provider: string; model: string }
/** Connection check outcome; provider response text is excluded. */
export interface ModelAccessVerifyValue { ok: boolean; errorCode?: string }
/** Draft discovery inputs delegated to the existing Host discovery seam. */
export interface ModelAccessDiscoverRequest { provider?: string; baseURL?: string; apiKey?: string; api?: string }
