/** A page-owned bounded authorization conversation; owner tokens remain process-local. */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Input, LinkIconMedium } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ModelAccessAttemptOwner, ModelAccessAttemptStatus, ModelAccessPrompt } from '@deepseek-ai/dsh-api-remotes/client'
import type { ModelAccessOperations } from './model-access-operations.ts'
import type { en } from './locales.ts'
import styles from './ModelAccessSection.module.css'
import modelStyles from './ModelsSection.module.css'

interface AttemptProps {
  owner: ModelAccessAttemptOwner
  operations: ModelAccessOperations
  t: (key: keyof typeof en) => string
  onSettled: (status: 'authorized' | 'cancelled' | 'failed') => void
  onClose: () => void
}

/**
 * Poll a single owned attempt only while its page is visible and within its TTL.
 * @param props - attempt capability and callbacks.
 * @returns authorization notice, prompt, and terminal result.
 */
export function ModelAuthorizationAttempt({ owner, operations, t, onSettled, onClose }: AttemptProps): ReactNode {
  const [status, setStatus] = useState<ModelAccessAttemptStatus>()
  const [readFailure, setReadFailure] = useState(false)
  const [cancelRequested, setCancelRequested] = useState(false)
  const [expired, setExpired] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [busy, setBusy] = useState(false)
  const active = useRef(true)
  const terminal = useRef(false)
  const answering = useRef(false)
  const withdrawing = useRef(false)
  const requestVersion = useRef(0)
  const reading = useRef(false)
  const reconciliationDeadline = useRef<number>()
  const pollNow = useRef<(() => Promise<void>) | undefined>()
  const expiresAt = useRef(Date.now() + 10 * 60 * 1000)
  const canContinue = (): boolean => active.current && !terminal.current
  const report = (next: ModelAccessAttemptStatus): void => {
    if (!canContinue()) return
    expiresAt.current = next.expiresAt
    setStatus(next)
    if (next.status !== 'pending') {
      terminal.current = true
      onSettled(next.status)
    }
  }
  useEffect(() => {
    active.current = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const schedule = (): void => {
      if (timer !== undefined) clearTimeout(timer)
      if (active.current && !terminal.current) timer = setTimeout(() => { void poll() }, 1000)
    }
    async function poll(): Promise<void> {
      if (timer !== undefined) clearTimeout(timer)
      if (!canContinue()) return
      if (reconciliationDeadline.current !== undefined && Date.now() >= reconciliationDeadline.current) {
        terminal.current = true
        setUncertain(true)
        setBusy(false)
        return
      }
      if (Date.now() >= expiresAt.current && reconciliationDeadline.current === undefined) {
        reconciliationDeadline.current = Date.now() + 30000
        setExpired(true)
        schedule()
        await cancel()
      }
      if (!canContinue()) return
      if (reading.current || answering.current || withdrawing.current) { schedule(); return }
      if (document.visibilityState !== 'hidden') {
        reading.current = true
        const version = requestVersion.current
        schedule()
        const response = await operations.status(owner)
        reading.current = false
        if (!canContinue()) return
        if (version === requestVersion.current && response.ok) {
          setReadFailure(false)
          report(response.value)
        }
        else if (version === requestVersion.current && !response.ok) {
          setReadFailure(true)
        }
      }
      schedule()
    }
    pollNow.current = poll
    void poll()
    return () => {
      active.current = false
      pollNow.current = undefined
      if (timer !== undefined) clearTimeout(timer)
      if (!terminal.current) void operations.cancel(owner)
    }
    // The capability is immutable for this keyed component; all other props
    // are stable callbacks from its account card.
  }, [owner, operations, onSettled])
  async function cancel(): Promise<void> {
    if (withdrawing.current || terminal.current) return
    setBusy(true)
    withdrawing.current = true
    requestVersion.current += 1
    setCancelRequested(true)
    const response = await operations.cancel(owner)
    withdrawing.current = false
    if (!canContinue()) return
    setBusy(answering.current)
    if (response.ok) {
      setReadFailure(false)
      report(response.value)
    } else {
      setReadFailure(true)
    }
  }
  async function respond(prompt: ModelAccessPrompt, value: string): Promise<void> {
    if (answering.current || withdrawing.current || terminal.current) return
    setBusy(true)
    answering.current = true
    const version = ++requestVersion.current
    const response = await operations.respond({ ...owner, promptId: prompt.promptId, value })
    answering.current = false
    if (!canContinue()) return
    setBusy(withdrawing.current)
    if (response.ok && (version === requestVersion.current || response.value.status !== 'pending')) report(response.value)
    else if (!response.ok && version === requestVersion.current) {
      setReadFailure(true)
    }
  }
  const notice = status?.notice
  let noticeUrl: string | undefined
  if (notice?.url !== undefined) {
    let parsed: URL | undefined
    try {
      parsed = new URL(notice.url)
    } catch (_error) {
      // A malformed supplier link cannot become an actionable browser URL.
    }
    if (parsed?.protocol === 'https:' || parsed?.protocol === 'http:') noticeUrl = notice.url
  }
  const settled = terminal.current
  const prompt = status?.prompt
  const messageKey = status?.status === 'authorized' ? 'accountAuthorized' : uncertain ? 'accountOutcomeUnknown'
    : status?.status === 'cancelled' ? expired ? 'accountExpired' : 'accountCancelled'
      : status?.status === 'failed' ? 'accountFailed' : expired ? 'accountExpiryPending'
        : cancelRequested ? 'accountCancelling' : 'accountPending'
  return <div className={styles['flow']}>
    <p className={styles['status']} role="status">{t(messageKey)}</p>
    {readFailure && !settled ? <div role="alert">
      <p className={styles['error']}>{t('accountTemporaryFailure')}</p>
      <Button variant="outline" disabled={busy} onClick={() => { void pollNow.current?.() }}>{t('retry')}</Button>
    </div> : null}
    {settled || notice === undefined ? null : <>
      <p className={styles['hint']}>{notice.message}</p>
      {noticeUrl === undefined ? null : <a className={styles['link']} href={noticeUrl} target="_blank" rel="noopener noreferrer">
        <LinkIconMedium kind="url" href={noticeUrl} />{t('accountOpenBrowser')}
      </a>}
      {notice.code === undefined ? null : <div>
        <p className={styles['hint']}>{t('accountDeviceCode')}</p>
        <pre className={styles['code']}>{notice.code}</pre>
      </div>}
    </>}
    {settled || cancelRequested || prompt === undefined ? null : <AuthorizationPromptForm
      key={prompt.promptId} prompt={prompt} busy={busy} t={t}
      onSubmit={(value) => { void respond(prompt, value) }}
    />}
    <div className={styles['actions']}>
      <Button variant="outline" disabled={busy} onClick={settled ? onClose : () => { void cancel() }}>
        {t(settled ? 'close' : 'cancel')}
      </Button>
    </div>
  </div>
}

function AuthorizationPromptForm({ prompt, busy, t, onSubmit }: {
  prompt: ModelAccessPrompt
  busy: boolean
  t: AttemptProps['t']
  onSubmit: (value: string) => void
}): ReactNode {
  const [answer, setAnswer] = useState('')
  const select = prompt.kind === 'select'
  return <form className={styles['flow']} onSubmit={(event) => {
    event.preventDefault()
    const value = answer
    setAnswer('')
    onSubmit(value)
  }}>
    <label className={styles['field']}>
      {prompt.message}
      {select ? <select className={`${styles['select']} ${modelStyles['selectInput']}`} value={answer} disabled={busy}
        onChange={(event) => { setAnswer(event.target.value) }}>
        <option value="">{t('accountChoose')}</option>
        {prompt.options.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
      </select> : <Input type={prompt.kind === 'secret' ? 'password' : 'text'}
        value={answer} disabled={busy} autoComplete="off" spellCheck={false}
        {...prompt.placeholder === undefined ? {} : { placeholder: prompt.placeholder }}
        onChange={(event) => { setAnswer(event.target.value) }}
      />}
    </label>
    {select && answer !== '' ? <p className={styles['hint']}>{prompt.options.find(option => option.id === answer)?.description}</p> : null}
    <div className={styles['actions']}>
      <Button type="submit" disabled={busy || answer.trim() === ''}>{t('accountRespond')}</Button>
    </div>
  </form>
}
