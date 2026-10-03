/** One provider's account lifecycle and independent request/configuration facts. */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ModelAccessAttemptOwner, ModelAccessConfiguration, ModelAccessProvider } from '@deepseek-ai/dsh-api-remotes/client'
import { ModelAuthorizationAttempt } from './ModelAuthorizationAttempt.tsx'
import { ModelAccountAction } from './ModelAccountAction.tsx'
import type { ModelAccessOperations } from './model-access-operations.ts'
import type { en } from './locales.ts'
import styles from './ModelAccessSection.module.css'
import modelStyles from './ModelsSection.module.css'

interface AccountCardProps {
  provider: ModelAccessProvider
  writable: boolean
  operations: ModelAccessOperations
  t: (key: keyof typeof en) => string
  onCommitted: (configuration: ModelAccessConfiguration) => void
  onRefresh: () => void
}

/**
 * Render one account row and explicit sign-in, enable, remove, and verify actions.
 * @param props - Host metadata and callbacks.
 * @returns provider card.
 */
export function ModelAccountCard({ provider, writable, operations, t, onCommitted, onRefresh }: AccountCardProps): ReactNode {
  const accountMethods = provider.methods.filter(choice => choice.id === 'oauth')
  const [method, setMethod] = useState(accountMethods[0]?.id ?? '')
  const [owner, setOwner] = useState<ModelAccessAttemptOwner>()
  const [authorized, setAuthorized] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<'start' | 'enable' | 'partial'>()
  const [action, setAction] = useState<'logout' | 'verify'>()
  const [verified, setVerified] = useState<boolean>()
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  // A changed Host profile invalidates earlier model-request evidence.
  useEffect(() => { setVerified(undefined) }, [provider.revision, provider.authentication, provider.record.configured])
  const settled = useCallback((status: 'authorized' | 'cancelled' | 'failed') => {
    setAuthorized(status === 'authorized')
    onRefresh()
  }, [onRefresh])
  async function start(): Promise<void> {
    setBusy(true)
    setFailure(undefined)
    setAuthorized(false)
    setVerified(undefined)
    const response = await operations.start({ provider: provider.id, method })
    if (!mounted.current) {
      if (response.ok) await operations.cancel(response.value)
      return
    }
    setBusy(false)
    if (response.ok) setOwner(response.value)
    else setFailure('start')
  }
  async function enable(): Promise<void> {
    setBusy(true)
    setFailure(undefined)
    const response = await operations.save({ provider: provider.id, expectedRevision: provider.revision, authentication: 'oauth' })
    if (!mounted.current) return
    setBusy(false)
    if (response.ok) {
      setOwner(undefined)
      setAuthorized(false)
      onCommitted(response.value)
    } else setFailure(response.configurationSaved === true ? 'partial' : 'enable')
  }
  const hasGrant = authorized || (provider.record.configured && provider.record.kind === 'grant')
  const authLabel = provider.authentication === 'oauth' ? t('accountAuthOAuth')
    : provider.authentication === 'api-key' ? t('accountAuthApiKey') : t('accountAuthAmbient')
  return (
    <div className={styles['card']}>
      <h3 className={styles['name']}>{provider.name}</h3>
      <div className={styles['facts']}>
        <span className={styles['status']}>{t(provider.configured ? 'accountEnabled' : 'accountDisabled')}</span>
        <span className={styles['status']}>{t(hasGrant || provider.record.configured ? 'accountRecordSaved' : 'accountRecordMissing')}</span>
        <span className={styles['status']}>{t(verified === true ? 'accountVerified' : verified === false ? 'accountVerifyFailed' : 'accountVerificationUnknown')}</span>
      </div>
      <p className={styles['hint']}>{t('accountEffectiveSource').replace('{source}', authLabel)}</p>
      {provider.credential === undefined ? null : <p className={styles['hint']}>
        {provider.credential.configured
          ? t('accountCredentialSource').replace('{source}', provider.credential.source ?? provider.credential.ref)
          : t('accountCredentialMissing')}
      </p>}
      {accountMethods.length === 1 ? <div className={styles['facts']}>
        <span className={styles['hint']}>{t('accountMethod')}</span>
        <span className={styles['hint']}>{accountMethods[0]?.label}</span>
      </div> : null}
      {accountMethods.length > 1 ? <label className={styles['field']}>
        {t('accountMethod')}
        <select className={`${styles['select']} ${modelStyles['selectInput']}`} value={method} disabled={busy || owner !== undefined} onChange={(event) => { setMethod(event.target.value) }}>
          {accountMethods.map(choice => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
        </select>
      </label> : null}
      {provider.inFlight && owner === undefined ? <p className={styles['hint']}>{t('accountBusy')}</p> : null}
      <div className={styles['actions']}>
        {accountMethods.length === 0 || owner !== undefined ? null : <Button
          variant="outline" disabled={!writable || busy || provider.inFlight || !provider.record.writable}
          onClick={() => { void start() }}
        >{t(busy ? 'accountActionPending' : 'accountSignIn')}</Button>}
        {hasGrant && !(provider.configured && provider.authentication === 'oauth') ? <Button
          disabled={!writable || !provider.editable || busy} onClick={() => { void enable() }}
        >{t(busy ? 'accountActionPending' : 'accountEnable')}</Button> : null}
        {hasGrant ? <Button
          variant="outline" disabled={!writable || !provider.record.writable || busy || (owner !== undefined && !authorized)}
          onClick={() => { setAction('logout') }}
        >{t('accountLogout')}</Button> : null}
        <Button variant="outline" disabled={!provider.configured || busy || (owner !== undefined && !authorized)}
          onClick={() => { setAction('verify') }}>{t('accountVerify')}</Button>
      </div>
      {hasGrant && !(provider.configured && provider.authentication === 'oauth') ? <p className={styles['hint']}>{t('accountEnableHint')}</p> : null}
      {failure === undefined ? null : <div role="alert">
        <p className={styles['error']}>{t(failure === 'partial' ? 'accountPartialSave' : failure === 'enable' ? 'accountEnableFailed' : 'accountFailed')}</p>
        {failure === 'start' ? null : <Button variant="outline" onClick={onRefresh}>{t('retry')}</Button>}
      </div>}
      {owner === undefined ? null : <ModelAuthorizationAttempt
        key={owner.attemptId} owner={owner} operations={operations} t={t} onSettled={settled}
        onClose={() => { setOwner(undefined) }}
      />}
      {action === undefined ? null : <ModelAccountAction
        action={action} provider={provider.id} operations={operations} t={t}
        onClose={() => { setAction(undefined) }}
        onCommitted={(next) => { setOwner(undefined); setAuthorized(false); onCommitted(next) }}
        onVerified={setVerified}
      />}
    </div>
  )
}
