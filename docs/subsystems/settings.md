# Plugin Configuration Forms

English | [中文](settings.zh.md)

The [settings service](../../packages/settings/settings/README.md) projects volatile Config fields from active profile entries. The [configuration editor](../../packages/boot/config-editor/README.md) persists edits through Cordis patches. Business consumers read `.get()` on their own Config references.

## Identity and values

A form namespace is the local id of a uniquely addressed entry in the active profile. Multiple plugin instances have separate forms when their entry ids differ. Ordinary fields are excluded. Descriptors carry resolved values, inherited values, explicit profile overrides, and an optimistic revision.

## Edits

`update` merges submitted fields. `replace` resets live fields to inherited configuration before applying submitted fields. `mutate` addresses individual paths, preserving secrets absent from a client response. Every write validates the complete Config and refuses stale revisions before persistence.

`settings/document-updated` invalidates form descriptors after Loader configuration changes. It is a UI notification; consumers use `loader/volatile-update` only when they need to refresh registration facts.

## Model account wire contracts

The `modelAccess` namespace projects Host configuration and credential metadata. Attempt owner tokens belong to the initiating page; subscription grants, API keys, and prompt answers are never returned.

```ts type-equiv
/** Host-generated identity of one authorization conversation. */
type ModelAccessAttemptId = Branded<'ModelAccessAttemptId'>
```

```ts type-equiv
/** Capability required to observe or change an authorization conversation. */
type ModelAccessOwnerToken = Branded<'ModelAccessOwnerToken'>
```

```ts type-equiv
/** Host-generated identity of the currently pending question. */
type ModelAccessPromptId = Branded<'ModelAccessPromptId'>
```

```ts type-equiv
/** Curated, non-secret editable provider profile. */
interface ModelAccessProfile {
  apiKeyEnv?: string
  displayName?: string
  api?: string
  baseURL?: string
  models?: { id: string; name?: string }[]
}
```

```ts type-equiv
/** Public provider configuration; credentials contain metadata only. */
interface ModelAccessProvider {
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
```

```ts type-equiv
/** Configuration returned to the model accounts page. */
interface ModelAccessConfiguration {
  writable: boolean
  providers: ModelAccessProvider[]
  custom?: { settingsNs: string; revision: number }
  apis: string[]
}
```

```ts type-equiv
/** Explicit save mode prevents subscription tokens reaching custom endpoints. */
interface ModelAccessSaveRequest {
  provider: string
  expectedRevision: number
  authentication: 'api-key' | 'oauth' | 'ambient'
  apiKey?: string
  baseURL?: string
  api?: string
  displayName?: string
  models?: { id: string; name?: string }[]
}
```

```ts type-equiv
/** Revision-checked provider removal. Credentials are retained. */
interface ModelAccessRemoveRequest { provider: string; expectedRevision: number }
```

```ts type-equiv
/** Credential action addressed to one provider. */
interface ModelAccessProviderRequest { provider: string }
```

```ts type-equiv
/** Authorization request naming one installed login method. */
interface ModelAccessStartRequest { provider: string; method: string }
```

```ts type-equiv
/** Private handle returned only to the initiating page. */
interface ModelAccessAttemptOwner { attemptId: ModelAccessAttemptId; ownerToken: ModelAccessOwnerToken }
```

```ts type-equiv
/** Wire-safe pending prompt; its signal remains within the Host. */
type ModelAccessPrompt = { promptId: ModelAccessPromptId; message: string } & (
  { kind: 'text' | 'secret'; placeholder?: string }
  | { kind: 'select'; options: { id: string; label: string; description?: string }[] }
)
```

```ts type-equiv
/** Current bounded authorization state; no answer or token is ever returned. */
interface ModelAccessAttemptStatus {
  attemptId: ModelAccessAttemptId
  provider: string
  status: 'pending' | 'authorized' | 'cancelled' | 'failed'
  expiresAt: number
  notice?: AuthorizationNotice
  prompt?: ModelAccessPrompt
  errorCode?: string
}
```

```ts type-equiv
/** Answer to the current question, consumed without being retained. */
interface ModelAccessRespondRequest extends ModelAccessAttemptOwner { promptId: ModelAccessPromptId; value: string }
```

```ts type-equiv
/** Explicit fixed-cost connection check. */
interface ModelAccessVerifyRequest { provider: string; model: string }
```

```ts type-equiv
/** Connection check outcome; provider response text is excluded. */
interface ModelAccessVerifyValue { ok: boolean; errorCode?: string }
```

```ts type-equiv
/** Draft discovery inputs delegated to the existing Host discovery seam. */
interface ModelAccessDiscoverRequest { provider?: string; baseURL?: string; apiKey?: string; api?: string }
```

<!-- BEGIN GENERATED cordis-surface (gen-cordis-catalog.ts) — do not edit between markers -->

<a id="cordis-surface"></a>

## Cordis API

Generated from source by `scripts/gen-cordis-catalog.ts` (verified fresh by `pnpm run verify-cordis-catalog` in doc-sync; regenerate with `pnpm run gen-cordis-catalog`) — the language sides differ only in locale-specific paired document paths. Signature blocks use a `ts cordis-catalog` fence and keep the original source JSDoc; dispatch modes are defined in the [primer](../cordis-primer.md#dispatch-modes), and the framework-inherited `ctx` API lives in [cordis-api/inherited.md](../cordis-api/inherited.md).

<a id="ctxmodelaccesscontroller--modelaccesscontroller"></a>

### `ctx.modelAccessController` — `ModelAccessController`

Model account Remote namespace; authentication answers and tokens remain on Host.

```ts cordis-catalog
/**
 * Read installed provider profiles and credential metadata.
 * @returns provider profiles and metadata, including installed dormant routes.
 */
@Remote async configuration(): Promise<ModelAccessConfiguration>

/**
 * Commit an explicit authentication mode and provider profile.
 * @param request - revision and explicit authentication mode.
 * @returns committed public configuration.
 */
@Remote async save(request: ModelAccessSaveRequest): Promise<ModelAccessConfiguration>

/**
 * Remove a provider from the writable configuration layer.
 * @param request - revision-checked removal; credentials are retained.
 * @returns current configuration.
 */
@Remote async remove(request: ModelAccessRemoveRequest): Promise<ModelAccessConfiguration>

/**
 * Delete page-managed API keys while preserving other credential sources.
 * @param request - provider whose page-managed key is cleared.
 * @returns remaining effective sources.
 */
@Remote async clearKey(request: ModelAccessProviderRequest): Promise<ModelAccessConfiguration>

/**
 * Delete the stored subscription grant without changing other credential sources.
 * @param request - provider whose stored subscription grant is deleted.
 * @returns remaining effective sources.
 */
@Remote async logout(request: ModelAccessProviderRequest): Promise<ModelAccessConfiguration>

/**
 * Start a private authorization attempt with bounded retention.
 * @param request - provider and its installed method.
 * @returns a private owner capability immediately.
 */
@Remote start(request: ModelAccessStartRequest): ModelAccessAttemptOwner

/**
 * Read the current state of an owned authorization attempt.
 * @param request - private attempt capability.
 * @returns latest prompt/notice and terminal result.
 */
@Remote status(request: ModelAccessAttemptOwner): ModelAccessAttemptStatus

/**
 * Consume one response to the currently owned prompt.
 * @param request - one answer to the current prompt.
 * @returns state after consuming the answer.
 */
@Remote respond(request: ModelAccessRespondRequest): ModelAccessAttemptStatus

/**
 * Request withdrawal and report the current settlement state.
 * @param request - private attempt capability.
 * @returns state while withdrawal settles.
 */
@Remote cancel(request: ModelAccessAttemptOwner): ModelAccessAttemptStatus

/**
 * Run the fixed small request through the active model adapter.
 * @param request - registered provider/model.
 * @param signal - caller lifetime.
 * @returns actual fixed-request outcome.
 */
@Remote async verify(request: ModelAccessVerifyRequest, signal: AbortSignal): Promise<ModelAccessVerifyValue>

/**
 * Discover model names through the installed Host adapter.
 * @param request - draft endpoint inputs.
 * @param signal - caller lifetime.
 * @returns discovered model names.
 */
@Remote async discover(request: ModelAccessDiscoverRequest, signal: AbortSignal): Promise<{ id: string; name?: string }[]>
```

Source: [`packages/api/settings-controller/src/model-access.ts`](../../packages/api/settings-controller/src/model-access.ts)

<a id="ctxsettings--settingsforms"></a>

### `ctx.settings` — `SettingsForms`

Project Config schemas into forms and own optional instance-level UI policy.

```ts cordis-catalog
/** Register the calling plugin instance's page policy without changing its Config.
 * @param presentation Automatic-page policy for this instance; `auto` defaults to true.
 * @param owner Plugin instance the policy belongs to; defaults to the calling fiber.
 * @returns Disposer; register it with the calling plugin's effects.
 * @throws If this instance already has a registered policy.
 */
configure(presentation: { auto?: boolean }, owner: Fiber = this.ctx.fiber): () => void

/** Locate the profile patch for native editing.
 * @returns The existing profile patch path.
 */
prepareDocument(): Promise<string>

/** Read active plugin schemas and their live values.
 * @param options Redaction required for remote callers.
 * @returns Forms keyed by unique profile entry ids.
 */
describe(options?: SettingsDescribeOptions): SettingsDescriptor[]

/** Merge editable fields into an entry's config.
 * @param ns Profile entry id.
 * @param patch Fields to merge.
 * @param expectedRevision Revision returned by describe.
 */
async update(ns: string, patch: object, expectedRevision?: number): Promise<void>

/** Reset all live fields, then set the supplied fields; ordinary config is preserved.
 * @param ns Profile entry id.
 * @param section Complete form values.
 * @param expectedRevision Revision returned by describe.
 */
async replace(ns: string, section: object, expectedRevision?: number): Promise<void>

/** Apply field edits without restating redacted secrets; unsetting an array index removes its element.
 * @param ns Profile entry id.
 * @param ops Ordered form edits.
 * @param expectedRevision Revision returned by describe.
 */
async mutate(ns: string, ops: readonly SettingsPathOp[], expectedRevision?: number): Promise<void>
```

Source: [`packages/settings/settings/src/index.ts`](../../packages/settings/settings/src/index.ts)

<a id="ctxsettingscontroller--settingscontroller"></a>

### `ctx.settingsController` — `SettingsController`

Host service backing the generated `ctx.remote.settings` namespace. Every remote read uses `redactSecrets: true`, so a `role('secret')` field cannot ride a response. Writes expose the settings service's merge, replacement, and path-addressed operations, and classify every provider refusal as `settings/conflict` or `settings/rejected` with the service's message.

```ts cordis-catalog
/**
 * Describe every registered namespace for a configuration page: redacted
 * layered values plus the serialized schema the page renders its form from.
 * @returns provider writability, local-document presence, and one view per namespace.
 * @throws RemoteError when no settings provider is mounted.
 */
@Remote describe(): SettingsDescribeValue

/**
 * Merge a patch into one namespace's stored user section.
 * @param ns - namespace key to write.
 * @param patch - fields to merge into the user section.
 * @param expectedRevision - revision the caller read; `undefined` writes unconditionally.
 * @returns the namespace's redacted view after the write.
 * @throws RemoteError when the request is invalid, no provider is mounted, or the provider refuses the write.
 */
@Remote update( ns: string, patch: Record<string, JsonValue>, expectedRevision: number | undefined, ): Promise<SettingsNamespaceView>

/**
 * Replace one namespace's stored user section wholesale.
 * @param ns - namespace key to write.
 * @param section - complete replacement user section.
 * @param expectedRevision - revision the caller read; `undefined` writes unconditionally.
 * @returns the namespace's redacted view after the write.
 * @throws RemoteError when the request is invalid, no provider is mounted, or the provider refuses the write.
 */
@Remote replace( ns: string, section: Record<string, JsonValue>, expectedRevision: number | undefined, ): Promise<SettingsNamespaceView>

/**
 * Apply path-addressed edits to one namespace's user section, resolved against
 * the section as stored rather than against whatever the caller last read,
 * then answer with that namespace's new redacted view.
 * @param ns - namespace key to write.
 * @param ops - the edits to apply, in order.
 * @param expectedRevision - revision the caller read; `undefined` writes unconditionally.
 * @returns the namespace's redacted view after the write.
 * @throws RemoteError when the request is invalid, no provider is mounted, or the provider refuses the write.
 */
@Remote async mutate( ns: string, ops: SettingsPathOpView[], expectedRevision: number | undefined, ): Promise<SettingsNamespaceView>

/**
 * Materialize the provider-owned settings document and open it in a native text editor.
 * @param signal - caller lifetime; abort terminates preparation or the native command.
 * @returns confirmation after the native opener accepts the document.
 * @throws RemoteError when no document exists, preparation fails, or opening fails.
 */
@Remote async openSettingsDocument(signal: AbortSignal): Promise<SettingsDocumentOpenValue>
```

Source: [`packages/api/settings-controller/src/index.ts`](../../packages/api/settings-controller/src/index.ts)

<a id="settings-events"></a>

### `settings/*` events

<a id="settingsdocument-updated--emit"></a>

#### `settings/document-updated` — emit

One profile entry's form values, availability, or page policy changed. Form clients re-read its schema, resolved values, and revision.

```ts cordis-catalog
/**
 * One profile entry's form values, availability, or page policy changed.
 * Form clients re-read its schema, resolved values, and revision.
 * @param ns Profile entry id.
 * @param revision The entry's new revision.
 * @mode emit
 */
'settings/document-updated'(ns: SettingsNamespace, revision: number): void
```

Source: [`packages/settings/settings/src/types.ts`](../../packages/settings/settings/src/types.ts)
<!-- END GENERATED cordis-surface -->
