/** Optional account contribution to the Models page, backed by explicit Host operations. */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { InjectFace, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type {} from './slot-contract.ts'
import { Button } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ModelAccessConfiguration } from '@deepseek-ai/dsh-api-remotes/client'
import type { ModelAccessOperations } from './model-access-operations.ts'
import { ModelAccountCard } from './ModelAccountCard.tsx'
import type { en } from './locales.ts'
import styles from './ModelAccessSection.module.css'

/** Plain account callbacks provided by the optional slot registration. */
export interface ModelAccessInjected {
  operations: ModelAccessOperations
  t: (key: keyof typeof en) => string
  onUpdated: () => void
}
/** The slot renderer flattens this registration's inject share. */
export type ModelAccessProps = InjectFace<ModelAccessInjected> & Pick<PropsRuntime<'settings.models.accounts'>, 'refreshKey'>

/**
 * Render account access without changing the API-key editors' state.
 * @param props - optional namespace callbacks and page refresh.
 * @returns account rows or a localized read failure.
 */
export function ModelAccessSection({ operations, t, onUpdated, refreshKey }: ModelAccessProps): ReactNode {
  const [configuration, setConfiguration] = useState<ModelAccessConfiguration>()
  const [failure, setFailure] = useState<string>()
  const [revision, setRevision] = useState(0)
  const requestVersion = useRef(0)
  useEffect(() => {
    let active = true
    const version = ++requestVersion.current
    void operations.configuration().then((response) => {
      if (!active || version !== requestVersion.current) return
      if (response.ok) {
        setConfiguration(response.value)
        setFailure(undefined)
      } else setFailure(response.errorCode === 'unavailable' ? 'accountUnavailable' : 'accountLoadFailed')
    })
    return () => { active = false }
  }, [operations, revision, refreshKey])
  const refresh = useCallback(() => { setRevision(previous => previous + 1) }, [])
  const committed = (next: ModelAccessConfiguration): void => {
    requestVersion.current += 1
    setConfiguration(next)
    setFailure(undefined)
    onUpdated()
  }
  if (configuration === undefined && failure === undefined) return null
  const providers = configuration?.providers.filter(provider =>
    provider.methods.some(method => method.id === 'oauth') || provider.configured || provider.record.configured,
  ) ?? []
  return (
    <section className={styles['section']} aria-label={t('accountTitle')}>
      <h2 className={styles['title']}>{t('accountTitle')}</h2>
      <p className={styles['hint']}>{t('accountHint')}</p>
      {failure === undefined ? <div className={styles['actions']}>
        <Button variant="outline" onClick={refresh}>{t('accountRefresh')}</Button>
      </div> : null}
      {failure === undefined ? null : <div role="alert">
        <p className={styles['error']}>{failure === 'accountUnavailable' ? t('accountUnavailable') : t('accountLoadFailed')}</p>
        <Button variant="outline" onClick={refresh}>{t('retry')}</Button>
      </div>}
      {providers.map(provider => <ModelAccountCard
        key={provider.id}
        provider={provider}
        writable={configuration?.writable === true}
        operations={operations}
        t={t}
        onCommitted={committed}
        onRefresh={refresh}
      />)}
    </section>
  )
}
