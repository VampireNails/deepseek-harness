/** Loader-only pending approval answerer for recorded-session deadline cases. */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-tools'
import type { ApprovalOutcome } from '@deepseek-ai/dsh-user-approval'

export const name = 'approval-wait-answerer-fixture'
export const inject = ['tools', 'approval']

/** Require approval for the owned write; release the answerer only on withdrawal. */
export function apply(ctx: Context): void {
  ctx.on('tools/pre-execute', async (exec, next) => exec.name === 'write'
    ? { kind: 'ask', reason: 'the owned write requires approval' }
    : next())
  ctx.on('approval/request', req => new Promise<ApprovalOutcome>((resolve) => {
    if (req.signal?.aborted) resolve('cancelled')
    else req.signal?.addEventListener('abort', () => { resolve('cancelled') }, { once: true })
  }))
}
