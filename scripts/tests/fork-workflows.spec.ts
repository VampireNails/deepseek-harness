/** Forks retain product checks without requiring the upstream organization's infrastructure. */
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { load } from 'js-yaml'
import { describe, expect, it } from 'vitest'

interface Step {
  name?: string
  if?: string
  uses?: string
  run?: string
  with?: Record<string, unknown>
  env?: Record<string, string>
}
interface Workflow {
  jobs: Record<string, { if?: string; env?: Record<string, string>; with?: Record<string, string>; needs?: string[]; 'runs-on': string; steps: Step[] }>
}

function workflow(file: string): Workflow {
  return load(readFileSync(resolve(import.meta.dirname, '../../.github/workflows', file), 'utf8')) as Workflow
}

// Only the canonical string/boolean cases below are evaluated, not arbitrary Actions expressions.
function evaluate(expression: string | undefined, repository: string, variables: Record<string, string> = {}): unknown {
  if (expression === undefined) return true
  const values: Record<string, string> = {
    'github.repository': repository,
    'github.event_name': 'pull_request',
    'github.event.pull_request.user.login': 'contributor',
    'github.event.pull_request.head.repo.full_name': repository,
    ...variables,
  }
  const source = expression.trim().replace(/^\$\{\{|\}\}$/g, '')
    .replace(/\b(?:github|vars)(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+/g, key => JSON.stringify(values[key] ?? ''))
  return runInNewContext(source, { fromJSON: JSON.parse }, { timeout: 1000 })
}

const upstream = 'deepseek-harness/deepseek-harness'
const fork = 'VampireNails/deepseek-harness'

describe('fork workflows', () => {
  it('limits Muse PR execution to Linux without changing other repositories', () => {
    const jobs = workflow('ci.yml').jobs
    for (const id of ['windows-build', 'windows-coverage', 'windows-native-tests']) {
      expect(evaluate(jobs[id]!.if, fork), id).toBeFalsy()
      expect(evaluate(jobs[id]!.if, upstream), id).toBeTruthy()
      expect(evaluate(jobs[id]!.if, 'another/fork'), id).toBeTruthy()
    }
    const targets = jobs['python-runtime']!.with!.targets
    expect(evaluate(targets, fork)).toBe('node24-linux-x64')
    expect(evaluate(targets, upstream)).toBe('node24-linux-x64,node24-win-x64')
    for (const id of ['node-24', 'node-24-coverage', 'node-24-bench', 'node-24-consumers', 'node-compat', 'python-sdk', 'python-runtime']) {
      expect(evaluate(jobs[id]!.if, fork), id).toBeTruthy()
    }
  })

  it('permits only deliberately skipped Windows needs in the Muse verdict', () => {
    const job = workflow('ci.yml').jobs['all-checks-passed']!
    const step = job.steps.find(item => item.name === 'Check Muse Linux job results')!
    expect(step).toBeDefined()
    expect(evaluate(step.if, fork)).toBeTruthy()
    expect(evaluate(step.if, upstream)).toBeFalsy()
    const windows = ['windows-build', 'windows-native-tests']
    const results = Object.fromEntries(job.needs!.map(id => [id, { result: windows.includes(id) ? 'skipped' : 'success' }]))
    const execute = (needs: typeof results): void => {
      runInNewContext(String(step.with!.script), {
        process: { env: { CI_JOB_RESULTS: JSON.stringify(needs) } },
        core: { setFailed(message: string) { throw new Error(message) }, info() {} },
      }, { timeout: 1000 })
    }
    expect(() => { execute(results) }).not.toThrow()
    for (const id of job.needs!) {
      for (const result of ['failure', 'cancelled', ...(windows.includes(id) ? [] : ['skipped'])]) {
        expect(() => { execute({ ...results, [id]: { result } }) }, `${id}: ${result}`).toThrow()
      }
    }
    expect(() => { execute({ ...results, 'node-24': { result: 'unknown' } }) }).toThrow()
    for (const id of job.needs!) {
      const incomplete = Object.fromEntries(Object.entries(results).filter(([name]) => name !== id))
      expect(() => { execute(incomplete) }, `${id}: missing`).toThrow()
    }
    expect(() => { execute({}) }).toThrow()
    expect(() => { execute({ ...results, 'windows-build': { result: 'success' } }) }).toThrow()
  })

  for (const [id, publicRunner, enterpriseRunner] of [
    ['node-24', 'ubuntu-24.04', 'dsh-ubuntu-24-04-16core'],
    ['node-24-coverage', 'ubuntu-24.04', 'dsh-ubuntu-24-04-16core'],
    ['node-24-consumers', 'ubuntu-24.04', 'dsh-ubuntu-24-04-16core'],
    ['windows-native-tests', 'windows-2025', 'dsh-windows-2025-16core'],
    ['windows-coverage', 'windows-2025', 'dsh-windows-2025-16core'],
    ['windows-build', 'windows-2025', 'dsh-windows-2025-16core'],
  ]) {
    it(`allocates ${id} on an available fork runner and preserves the upstream pool`, () => {
      const job = workflow('ci.yml').jobs[id!]!
      expect(job, 'owning CI job exists').toBeDefined()
      expect(evaluate(job['runs-on'], fork)).toBe(publicRunner)
      expect(evaluate(job['runs-on'], upstream)).toBe(enterpriseRunner)
      const variable = id!.startsWith('windows') ? 'vars.DSH_CI_FAILOVER_WINDOWS' : 'vars.DSH_CI_FAILOVER_LINUX'
      const pool = id!.startsWith('windows') ? ['self-hosted', 'dsh-win-ci', 'windows'] : ['self-hosted', 'linux', 'x64', 'vm-backup']
      expect(evaluate(job['runs-on'], fork, { [variable]: 'selfhosted' })).toEqual(pool)
      expect(evaluate(job['runs-on'], fork, { [variable]: 'blacksmith' })).toMatch(/^blacksmith-/u)
      expect(evaluate(job['runs-on'], fork, { [variable]: 'selfhosted', 'github.event.pull_request.user.login': 'dependabot[bot]' })).toBe(publicRunner)
    })
  }

  it('does not call upstream Issue Project automation from a fork', () => {
    for (const [file, id] of [['issue-policy.yml', 'policy'], ['issue-lifecycle.yml', 'lifecycle']]) {
      const condition = workflow(file!).jobs[id!]!.if
      expect(evaluate(condition, fork)).toBeFalsy()
      expect(evaluate(condition, upstream)).toBeTruthy()
    }
  })

  it('builds and preserves preview artifacts even when fork deployment is not configured', () => {
    const job = workflow('build-preview-cloudflare.yml').jobs.preview!
    const steps = job.steps
    for (const name of ['Build workspace', 'Build the preview page and pack the VFS image', 'Shape the upload']) {
      expect(evaluate(steps.find(step => step.name === name)!.if, fork)).toBeTruthy()
    }
    const artifact = steps.find(step => step.uses === 'actions/upload-artifact@v4')
    expect(artifact?.with).toMatchObject({ path: 'apps/web/dist', 'if-no-files-found': 'error' })
    for (const name of ['Upload to Cloudflare Pages', 'Verify the protected deployment serves the image', 'Comment the preview URL']) {
      const condition = steps.find(step => step.name === name)!.if
      expect(condition).toBe("steps.deployment.outputs.enabled == 'true'")
    }
    const enabled = job.env!.DEPLOY_PREVIEW
    expect(evaluate(enabled, fork)).toBeFalsy()
    expect(evaluate(enabled, fork, { 'vars.DSH_PREVIEW_DEPLOY_ENABLED': 'true' })).toBeTruthy()
    expect(evaluate(enabled, upstream)).toBeTruthy()
    expect(evaluate(enabled, upstream, { 'github.event.pull_request.head.repo.full_name': fork })).toBeFalsy()
    expect(evaluate(enabled, upstream, { 'github.event.pull_request.user.login': 'dependabot[bot]' })).toBeFalsy()
    const credentials = steps.find(step => step.name === 'Check preview deployment credentials')!
    expect(credentials.if).toBe("env.DEPLOY_PREVIEW == 'true'")
  })

  it('rejects every incomplete credential set before enabling deployment', (test) => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-preview-credentials-'))
    test.onTestFinished(() => { rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }) })
    const step = workflow('build-preview-cloudflare.yml').jobs.preview!.steps.find(item => item.name === 'Check preview deployment credentials')!
    const names = ['CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET']
    for (const missing of [...names, undefined]) {
      const output = join(root, 'output')
      writeFileSync(output, '')
      const environment = { ...process.env, GITHUB_OUTPUT: output, ...Object.fromEntries(names.map(name => [name, name === missing ? '' : 'fixture'])) }
      const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash'
      const result = spawnSync(bash, ['--noprofile', '--norc', '-e', '-c', step.run!], { env: environment, encoding: 'utf8' })
      if (result.error !== undefined) throw result.error
      expect(result.status).toBe(missing === undefined ? 0 : 1)
      expect(readFileSync(output, 'utf8')).toBe(missing === undefined ? 'enabled=true\n' : '')
    }
  })
})
