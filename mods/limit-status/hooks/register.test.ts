import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import { coldResume, resumeSavedText } from './register.ts'

const STATE = '/data/afk-hook.json'
const MARKER = 'limitswitcher:jev-compact'

type World = { posts: { path: string; body: any }[]; compactions: string[]; statuses: (string | undefined)[] }

/**
 * The engine beneath the mod: LimitSwitcher's state file and local API, and (standing in for the
 * jev-compact plugin) a compaction hook that answers each try with the next of `outcomes`.
 */
function world(on: On, options: { compact?: string | null; outcomes?: ({ skip: string } | { saved: number })[]; noApp?: boolean; alerts?: { id: string; text: string }[]; env?: Record<string, string>; jevOff?: boolean; oldApp?: boolean } = {}): World {
  const w: World = { posts: [], compactions: [], statuses: [] }
  const clock = mock.clock(on)
  let asked = false
  on('fs.read', () => {
    if (options.noApp) throw new Error('ENOENT') // LimitSwitcher is not running
    return { value: JSON.stringify({ url: 'http://127.0.0.1:9/api/afk', token: 'tok' }) }
  })
  on('session.id', () => ({ value: 'session-1' }))
  on('session.model', () => ({ value: 'Opus 5.5' }))
  mock.env(on, options.env ?? {})
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1000, tokens: 10, percent: 1 }, rateLimits: [] } }))
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('classic.SessionStart', () => ({}))
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
      answer = { line: null, compact: options.compact && !asked ? { id: options.compact } : null, alerts: options.alerts ?? [] }
      if (options.compact) asked = true
    }
    if (path === '/api/compaction' && body.resume) answer = options.oldApp ? { ok: true } : { ok: !options.jevOff, resume: true }
    if (path === '/api/limits') answer = { text: '**⇄ a@example.com** · Opus 5.5 (high)' }
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
    expect(w.statuses).toEqual([]) // shown in LimitSwitcher's own line, not a band of the mod's
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

  test('/jevcompact runs the compaction by hand and LimitSwitcher\'s line shows it', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { outcomes: [{ saved: 30_000 }, { skip: 'no OpenRouter key (OPENROUTER_API_KEY)' }] })
    const registered: string[] = []
    const toasts: string[] = []
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('ui.toast', ($, e) => { toasts.push(String(e.text)); return { value: undefined } })
    on('command.register', ($, e) => { registered.push(e.name); return { value: { command: e.name } } })
    await $.session.start({ cwd: '/' })
    expect(registered).toContain('jevcompact')
    w.posts.length = 0
    const started = await $.command.run({ command: 'jevcompact' })
    expect(started.text).toBe('Jev compacting…')
    await (w as any).clock.settle()
    const told = w.posts.filter((p) => p.path === '/api/compaction').map((p) => p.body)
    expect(told[0]).toMatchObject({ session: 'session-1', outcome: 'running' })  // before it starts: the line says "Jev Compacting…"
    expect(told[1]).toMatchObject({ session: 'session-1', id: told[0].id, outcome: 'done', saved: 30_000 })
    expect(toasts).toEqual([])                                                   // the line says what it saved
    expect(w.statuses).toEqual([])
    await $.command.run({ command: 'jevcompact' })
    await (w as any).clock.settle()
    expect(toasts).toEqual(['No Jev compaction: no OpenRouter key (OPENROUTER_API_KEY)'])
    expect(w.compactions).toEqual([MARKER, MARKER])
  })

  test('/jevcompact without LimitSwitcher running says how it went in a toast', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { outcomes: [{ saved: 30_000 }], noApp: true })
    const toasts: string[] = []
    on('ui.toast', ($, e) => { toasts.push(String(e.text)); return { value: undefined } })
    await $.command.run({ command: 'jevcompact' })
    await (w as any).clock.settle()
    expect(toasts).toEqual(['Jev saved ~30k tokens'])
  })

  test('shows each reset alert from the app as a toast, once', { options: { statePath: STATE } }, async ($, on) => {
    const text = '⇄ LimitSwitcher · Claude has room again: second@example.com\'s limit has reset'
    world(on, { alerts: [{ id: 'reset-1', text }] })
    const toasts: string[] = []
    on('ui.toast', ($, e) => { toasts.push(e.text); return { value: undefined } })
    await measure($)
    await $.session.measure({
      context: { window: 1_000_000, tokens: 200_000, percent: 20 },
      rateLimits: [{ kind: 'five_hour', percentUsed: 10, resetsAt: '2026-10-02T22:00:00Z' }],
      changed: ['rateLimits' as const],
    })
    expect(toasts).toEqual([text])
  })

  test('/limits answers with every Claude account from the app, without a model turn', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on)
    const registered: string[] = []
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('command.register', ($, e) => { registered.push(e.name); return { value: { command: e.name } } })
    await $.session.start({ cwd: '/' })
    expect(registered).toContain('limits')
    const answer = await $.command.run({ command: 'limits' })
    expect(answer.text).toBe('**⇄ a@example.com** · Opus 5.5 (high)')
    const sent = w.posts.find((p) => p.path === '/api/limits')!
    expect(sent.body).toEqual({ session: 'session-1', model: 'Opus 5.5', context: { tokens: 10, window: 1000, percent: 1 } })
  })

  test('/limits in a window with an account of its own sends its folder', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { env: { CLAUDE_CONFIG_DIR: '/data/profiles/window-2' } })
    await $.command.run({ command: 'limits' })
    expect(w.posts.find((p) => p.path === '/api/limits')!.body.configDir).toBe('/data/profiles/window-2')
  })

  test('/limits on an app from before /limits says to update it', { options: { statePath: STATE } }, async ($, on) => {
    on('fs.read', () => ({ value: JSON.stringify({ url: 'http://127.0.0.1:9/api/afk', token: 'tok' }) }))
    on('session.id', () => ({ value: 'session-1' }))
    on('session.model', () => ({ value: 'Opus 5.5' }))
    mock.env(on, {})
    on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1000 }, rateLimits: [] } }))
    on('http.fetch', () => ({ value: { status: 400, ok: false, headers: {}, text: '{"error":"Unknown route"}' } }))
    const answer = await $.command.run({ command: 'limits' })
    expect(answer.text).toBe('This LimitSwitcher is too old for /limits: update it (Settings → Update).')
  })

  test('/limits without LimitSwitcher running says so', { options: { statePath: STATE } }, async ($, on) => {
    world(on, { noApp: true })
    const answer = await $.command.run({ command: 'limits' })
    expect(answer.text).toBe('LimitSwitcher isn\'t running.')
  })

  test('without LimitSwitcher running, nothing happens', { options: { statePath: STATE } }, async ($, on) => {
    on('fs.read', () => { throw new Error('ENOENT') })
    on('session.measure', ($, e) => ({ changed: e.changed }))
    const result = await measure($)
    expect(result.changed).toEqual(['rateLimits'])
  })
  const resumed = { source: 'resume' as const, session_id: 'old-1', context_tokens: 666_000, prompt_cache_likely_expired: true, seconds_since_last_response: 470_000 }

  test('a cold resume is one worth compacting first', () => {
    expect(coldResume(resumed)).toBe(true)
    expect(coldResume({ ...resumed, source: 'startup' })).toBe(false)
    expect(coldResume({ ...resumed, prompt_cache_likely_expired: false })).toBe(false)   // still cached: cheap
    expect(coldResume({ ...resumed, context_tokens: 40_000 })).toBe(false)              // too small to matter
    expect(coldResume({ ...resumed, agent_id: 'a1' })).toBe(false)
    expect(resumeSavedText(289_000, 512_000, 666_000)).toBe(
      '⇄ LimitSwitcher · Jev saved ~289k of 666k tokens (56%) before this resume: it uses about 56% less of your limit than Claude Code said')
  })

  test('an old session resumed: Jev compacts it first, the first message waits, then goes', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on, { outcomes: [{ saved: 20_000 }] })
    const toasts: string[] = []
    const sent: string[] = []
    on('ui.toast', ($, e) => { toasts.push(String(e.text)); return { value: undefined } })
    on('prompt.submit', ($, e) => { sent.push(e.text); return { text: e.text } })
    await $.classic.SessionStart(resumed)
    const first = await $.prompt.submit({ text: 'where were we?' })        // typed before Jev is done
    expect(first.drop).toContain('Jev is compacting')
    expect(sent).toEqual([])
    await (w as any).clock.settle()
    expect(w.compactions).toEqual([MARKER])
    const told = w.posts.filter((p) => p.path === '/api/compaction').map((p) => p.body)
    expect(told[0]).toMatchObject({ session: 'old-1', outcome: 'running', resume: true })
    expect(told[1]).toMatchObject({ session: 'old-1', id: told[0].id, outcome: 'done', saved: 20_000 })
    expect(toasts).toEqual([resumeSavedText(20_000, 50_000, 666_000)])
    expect(sent).toEqual(['where were we?'])                               // sent once it was done
    await $.prompt.submit({ text: 'next' })
    expect(sent).toEqual(['where were we?', 'next'])                       // nothing held after
  })

  for (const [name, option] of [['Jev compaction off', { jevOff: true }], ['an app from before', { oldApp: true }]] as const) {
    test(`with ${name}, a resume goes on as usual`, { options: { statePath: STATE } }, async ($, on) => {
      const w = world(on, option)
      await $.classic.SessionStart(resumed)
      await (w as any).clock.settle()
      expect(w.compactions).toEqual([])
    })
  }

  test('a resume still cached, or a new session, is left alone', { options: { statePath: STATE } }, async ($, on) => {
    const w = world(on)
    await $.classic.SessionStart({ ...resumed, prompt_cache_likely_expired: false })
    await $.classic.SessionStart({ source: 'startup' })
    await (w as any).clock.settle()
    expect(w.compactions).toEqual([])
    expect(w.posts.filter((p) => p.path === '/api/compaction')).toEqual([])
  })
})
