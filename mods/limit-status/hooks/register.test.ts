import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const STATE = '/data/afk-hook.json'
const MARKER = 'limitswitcher:jev-compact'

type World = { posts: { path: string; body: any }[]; compactions: string[]; statuses: (string | undefined)[] }

/**
 * The engine beneath the mod: LimitSwitcher's state file and local API, and (standing in for the
 * jev-compact plugin) a compaction hook that answers each try with the next of `outcomes`.
 */
function world(on: On, options: { compact?: string | null; outcomes?: ({ skip: string } | { saved: number })[] } = {}): World {
  const w: World = { posts: [], compactions: [], statuses: [] }
  const clock = mock.clock(on)
  let asked = false
  on('fs.read', () => ({ value: JSON.stringify({ url: 'http://127.0.0.1:9/api/afk', token: 'tok' }) }))
  on('session.id', () => ({ value: 'session-1' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1000, tokens: 10, percent: 1 }, rateLimits: [] } }))
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('ui.status', ($, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
  on('http.fetch', ($, e) => {
    const path = e.url.replace('http://127.0.0.1:9', '')
    const body = JSON.parse(e.init?.body ?? '{}')
    w.posts.push({ path, body })
    let answer: unknown = { ok: true }
    if (path === '/api/statusline') {
      answer = { line: null, compact: options.compact && !asked ? { id: options.compact } : null }
      if (options.compact) asked = true
    }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(answer) } }
  })
  const outcomes = [...(options.outcomes ?? [])]
  on('session.compact', ($, e) => {
    w.compactions.push(String(e.instructions))
    const next = outcomes.shift() ?? { saved: 1000 }
    if ('skip' in next) return { skip: next.skip }
    return { messages: [{ role: 'user' as const, text: 'kept', toolUses: [] }], tokensBefore: 50_000, tokensAfter: 50_000 - next.saved }
  })
  ;(w as any).clock = clock
  return w
}

const measure = ($: any) => $.session.measure({
  context: { window: 1_000_000, tokens: 200_000, percent: 20 },
  rateLimits: [{ kind: 'five_hour', percentUsed: 100, resetsAt: '2026-10-02T22:00:00Z' }],
  changed: ['rateLimits' as const],
})

describe('limit-status', () => {
  test('feeds the usage to the app, and draws nothing of its own', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on)
    await measure($)
    const sent = w.posts.find((p) => p.path === '/api/statusline')!
    expect(sent.body.source).toBe('mod')
    expect(sent.body.session).toBe('session-1')
    expect(sent.body.rate_limits.five_hour.used_percentage).toBe(100)
    expect(w.compactions).toEqual([])
  })

  test('runs the compaction the app asks for, with the marker, and reports what it saved', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { compact: 'job-1', outcomes: [{ saved: 20_000 }] })
    await measure($)
    await (w as any).clock.settle()
    expect(w.compactions).toEqual([MARKER])
    const done = w.posts.find((p) => p.path === '/api/compaction')!
    expect(done.body).toEqual({ session: 'session-1', id: 'job-1', outcome: 'done', saved: 20_000 })
    expect(w.statuses[0]).toContain('Jev compacting')
    expect(w.statuses.some((s) => s?.includes('~20k tokens less'))).toBe(true)
  })

  test('Jev failing: waits 30 s and tries again, three tries in all, then gives up', { options: { statePath: STATE } }, async ($, on) => {
    const fail = { skip: 'Jev failed: 503' }
    const w = world(on, { compact: 'job-2', outcomes: [fail, fail, fail] })
    await measure($)
    await (w as any).clock.settle()
    expect(w.compactions).toHaveLength(1)
    await (w as any).clock.advance(29_000)
    expect(w.compactions).toHaveLength(1)
    await (w as any).clock.advance(1_000)
    expect(w.compactions).toHaveLength(2)
    await (w as any).clock.advance(30_000)
    expect(w.compactions).toHaveLength(3)
    await (w as any).clock.advance(60_000)
    expect(w.compactions).toHaveLength(3)
    const done = w.posts.find((p) => p.path === '/api/compaction')!
    expect(done.body.outcome).toBe('failed')
    expect(done.body.reason).toContain('503')
  })

  test('Jev failing once, then working', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { compact: 'job-3', outcomes: [{ skip: 'Jev failed: timeout' }, { saved: 5_000 }] })
    await measure($)
    await (w as any).clock.advance(30_000)
    expect(w.compactions).toHaveLength(2)
    expect(w.posts.find((p) => p.path === '/api/compaction')!.body.outcome).toBe('done')
  })

  test('a final skip (no key, nothing to prune) is not retried', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { compact: 'job-4', outcomes: [{ skip: 'no OpenRouter key (OPENROUTER_API_KEY)' }] })
    await measure($)
    await (w as any).clock.advance(120_000)
    expect(w.compactions).toHaveLength(1)
    const done = w.posts.find((p) => p.path === '/api/compaction')!
    expect(done.body.outcome).toBe('skipped')
  })


  test('the same request is run once', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { compact: 'job-6' })
    await measure($)
    await (w as any).clock.settle()
    await $.session.measure({
      context: { window: 1_000_000, tokens: 200_000, percent: 20 },
      rateLimits: [{ kind: 'five_hour', percentUsed: 99, resetsAt: '2026-10-02T22:00:00Z' }],
      changed: ['rateLimits' as const],
    })
    await (w as any).clock.settle()
    expect(w.compactions).toHaveLength(1)
  })

  test('/jevcompact runs the compaction by hand and says what it saved', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { outcomes: [{ saved: 30_000 }, { skip: 'no OpenRouter key (OPENROUTER_API_KEY)' }] })
    const registered: string[] = []
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('ui.toast', () => ({ value: undefined }))
    on('command.register', ($, e) => { registered.push(e.name); return { value: { command: e.name } } })
    await $.session.start({ cwd: '/' })
    expect(registered).toContain('jevcompact')
    const started = await $.command.run({ command: 'jevcompact' })
    expect(started.text).toBe('Jev compacting…')
    await (w as any).clock.settle()
    expect(w.statuses.at(-1)).toBe('⇄ LimitSwitcher · Jev compacted: ~30k tokens less to load')
    await $.command.run({ command: 'jevcompact' })
    await (w as any).clock.settle()
    expect(w.statuses.at(-1)).toBe('⇄ LimitSwitcher · No Jev compaction: no OpenRouter key (OPENROUTER_API_KEY)')
    expect(w.compactions).toEqual([MARKER, MARKER])
  })

  test('without LimitSwitcher running, nothing happens', { options: { statePath: STATE } }, async ($, on) => {
    on('fs.read', () => { throw new Error('ENOENT') })
    on('session.measure', ($, e) => ({ changed: e.changed }))
    const result = await measure($)
    expect(result.changed).toEqual(['rateLimits'])
  })
})
