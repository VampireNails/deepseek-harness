/** Confirmation for Host credential removal or an explicit paid-quota model probe. */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Modal } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ModelAccessConfiguration } from '@deepseek-ai/dsh-api-remotes/client'
import type { ModelAccessOperations } from './model-access-operations.ts'
import type { en } from './locales.ts'
import styles from './ModelAccessSection.module.css'
import modelStyles from './ModelsSection.module.css'

interface ActionProps {
  action: 'logout' | 'verify'
  provider: string
  operations: ModelAccessOperations
  t: (key: keyof typeof en) => string
  onClose: () => void
  onCommitted: (configuration: ModelAccessConfiguration) => void
  onVerified: (verified: boolean) => void
}

/**
 * Confirm model usage or destructive Host record removal before invoking it.
 * @param props - selected account action and result callbacks.
 * @returns confirmation modal retaining failures for retry.
 */
export function ModelAccountAction({ action, provider, operations, t, onClose, onCommitted, onVerified }: ActionProps): ReactNode {
  const [models, setModels] = useState<readonly { id: string; name?: string }[]>()
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    if (action !== 'verify') return
    let active = true
    void operations.models(provider).then((response) => {
      if (!active) return
      if (response.ok) {
        setModels(response.value)
        setModel(response.value[0]?.id ?? '')
      } else {
        setModels([])
        setFailure(true)
      }
    })
    return () => { active = false }
  }, [action, provider, operations])
  const logout = action === 'logout'
  async function confirm(): Promise<void> {
    setBusy(true)
    setFailure(false)
    if (logout) {
      const response = await operations.logout({ provider })
      if (!mounted.current) return
      setBusy(false)
      if (response.ok) {
        onCommitted(response.value)
        onClose()
      } else setFailure(true)
    } else {
      const response = await operations.verify({ provider, model })
      if (!mounted.current) return
      setBusy(false)
      if (response.ok && response.value.ok) {
        onVerified(true)
        onClose()
      } else {
        onVerified(false)
        setFailure(true)
      }
    }
  }
  return <Modal
    open title={t(logout ? 'accountLogoutTitle' : 'accountVerifyTitle')}
    closeLabel={t('close')} onClose={() => { if (!busy) onClose() }}
    description={t(logout ? 'accountLogoutDescription' : 'accountVerifyDescription')}
    footer={<>
      <Button variant="outline" disabled={busy} onClick={onClose}>{t('cancel')}</Button>
      <Button disabled={busy || (!logout && model === '')} onClick={() => { void confirm() }}>
        {t(busy ? 'accountActionPending' : logout ? 'accountLogoutConfirm' : 'accountVerifyConfirm')}
      </Button>
    </>}
  >
    {logout ? null : <label className={styles['field']}>
      {t('model')}
      <select className={`${styles['select']} ${modelStyles['selectInput']}`} value={model} disabled={busy || models === undefined || models.length === 0}
        onChange={(event) => { setModel(event.target.value) }}>
        {models?.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.name ?? candidate.id}</option>)}
      </select>
    </label>}
    {!logout && models?.length === 0 ? <p className={styles['hint']}>{t('accountModelsEmpty')}</p> : null}
    {failure ? <p className={styles['error']} role="alert">{t(logout ? 'accountLogoutFailed' : 'accountVerifyFailed')}</p> : null}
  </Modal>
}
