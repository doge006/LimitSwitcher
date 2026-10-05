import type { Register } from 'claude-code'

// Feeds Claude Code's live usage to LimitSwitcher straight from Claude Code, after every turn
// (and whenever a window moves a point), so the app's numbers are current without asking the API.
// LimitSwitcher's line itself is in Claude Code's status line (the app's status line command).
//
// It also runs the Jev compaction LimitSwitcher asks for when this session hit a usage limit and
// its account was swapped: the jev-compact plugin shrinks the tool outputs the session no longer
// needs, so the new account (which has none of it cached) loads less. Claude Code skips a
// plugin's own compaction hook when that plugin starts the compaction, so the two are separate.
// `/jevcompact` runs the same compaction by hand (registered here for that reason too).
const EVERY = 30_000 // an idle session still reports now and then, and picks up a compaction asked for
const SOON = [2_000, 5_000, 10_000, 20_000, 40_000] // after a turn that ended on an error (a usage limit): look sooner

// Shared with mods/jev-compact/hooks/register.ts (plugins can't import each other).
const MARKER = 'limitswitcher:jev-compact'
const FAILED = 'Jev failed: '

const TRIES = 3          // Jev failed: wait, try again, wait, one more time, then give up
const RETRY_AFTER = 30_000
const BUSY_TRIES = 6     // a turn is running (the session went on, or is just ending): look again shortly
const BUSY_AFTER = 5_000

type Window = { kind: string; percentUsed: number; resetsAt?: string }
type App = { base: string; token: string }

const handled = new Set<string>() // compaction ids already run (the app keeps asking until it hears back)

async function appOf($: any, statePath: string): Promise<App | null> {
  if (!statePath) return null
  try {
    const state = JSON.parse(await $.fs.read(statePath))
    return { base: String(state.url).replace(/\/api\/afk$/, ''), token: String(state.token) }
  } catch {
    return null // LimitSwitcher is not running
  }
}

async function post($: any, app: App, path: string, body: unknown): Promise<any> {
  const reply = await $.http.fetch(`${app.base}${path}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${app.token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return reply.ok ? JSON.parse(reply.text) : null
}

async function report($: any, statePath: string, rateLimits: readonly Window[]): Promise<void> {
  const app = await appOf($, statePath)
  if (!app) return
  const limits: Record<string, { used_percentage: number; resets_at?: number }> = {}
  for (const w of rateLimits) {
    if (w.kind !== 'five_hour' && w.kind !== 'seven_day') continue
    limits[w.kind] = { used_percentage: w.percentUsed, ...(w.resetsAt ? { resets_at: Math.floor(Date.parse(w.resetsAt) / 1000) } : {}) }
  }
  try {
    const session = String(await $.session.id())
    const answer = await post($, app, '/api/statusline', {
      rate_limits: Object.keys(limits).length ? limits : null, session, source: 'mod',
    })
    const id = answer?.compact?.id
    if (typeof id === 'string' && id && !handled.has(id)) {
      handled.add(id)
      $.clock.after(0, () => compactFor($, statePath, session, id, 1, 1)) // outside this dispatch: never under a turn
    }
  } catch {
    // the app is busy: the next report tries again
  }
}

async function poll($: any, statePath: string): Promise<void> {
  const { rateLimits } = await $.session.usage()
  await report($, statePath, rateLimits)
}

/** Runs the compaction, retrying as the app's rule says, then tells the app how it went. What it
 * is doing shows in the app's own line in the status bar ("Jev Compacting…", then what it saved). */
async function compactFor($: any, statePath: string, session: string, id: string, attempt: number, busy: number): Promise<void> {
  let skip: string | undefined
  let saved = 0
  try {
    const result = await $.session.compact({ instructions: MARKER })
    skip = result.skip
    if (skip === undefined && typeof result.tokensBefore === 'number' && typeof result.tokensAfter === 'number') {
      saved = Math.max(0, Math.round(result.tokensBefore - result.tokensAfter))
    }
  } catch (error) {
    // Refused while a turn runs: the limit's turn may still be ending, or the session went on
    if (busy < BUSY_TRIES) {
      $.clock.after(BUSY_AFTER, () => compactFor($, statePath, session, id, attempt, busy + 1))
      return
    }
    skip = `the session is busy (${error instanceof Error ? error.message : String(error)})`
  }
  if (skip !== undefined && skip.startsWith(FAILED) && attempt < TRIES) {
    $.clock.after(RETRY_AFTER, () => compactFor($, statePath, session, id, attempt + 1, 1))
    return
  }
  const outcome = skip === undefined ? 'done' : skip.startsWith(FAILED) ? 'failed' : 'skipped'
  const app = await appOf($, statePath)
  if (!app) return
  try {
    await post($, app, '/api/compaction', { session, id, outcome, saved, ...(skip ? { reason: skip.slice(0, 300) } : {}) })
  } catch {
    // the app gives up waiting on its own (and the session goes on)
  }
}

/** `/jevcompact`: the same compaction by hand, once, in the session it is typed in. The app is told
 * when it starts and how it went, so its line in the status bar shows it like one before a swap.
 * Returns what to say, and whether the app showed it (then nothing else needs to). */
async function compactByHand($: any, statePath: string): Promise<{ text: string; shown: boolean }> {
  const app = await appOf($, statePath)
  const id = `hand-${Date.now().toString(36)}`
  let session = ''
  let shown = false
  if (app) {
    try {
      session = String(await $.session.id())
      shown = (await post($, app, '/api/compaction', { session, id, outcome: 'running' }))?.ok === true
    } catch {
      // the app is busy: the toast says how it went
    }
  }
  let text: string
  let outcome = 'skipped'
  let saved = 0
  let skip: string | undefined
  try {
    const result = await $.session.compact({ instructions: MARKER })
    skip = result.skip
    if (skip === undefined) {
      saved = typeof result.tokensBefore === 'number' && typeof result.tokensAfter === 'number'
        ? Math.max(0, Math.round(result.tokensBefore - result.tokensAfter)) : 0
      outcome = 'done'
      text = `Jev compacted: ~${Math.round(saved / 1000)}k tokens less to load`
    } else {
      outcome = skip.startsWith(FAILED) ? 'failed' : 'skipped'
      text = `No Jev compaction: ${skip}`
    }
  } catch (error) {
    skip = error instanceof Error ? error.message : String(error)
    text = `No Jev compaction: ${skip}`
  }
  if (app && shown) {
    try {
      await post($, app, '/api/compaction', { session, id, outcome, saved, ...(skip ? { reason: skip.slice(0, 300) } : {}) })
    } catch {
      // the app stops showing it on its own
    }
  }
  return { text, shown: shown && outcome === 'done' }
}

export const register: Register = (on, options) => {
  const statePath = String(options.statePath ?? '')
  let last = '' // what was last sent: nothing new, nothing to send again

  on('session.measure', async ($, e, next) => {
    const result = await next(e)
    const key = JSON.stringify(e.rateLimits)
    if (key !== last) {
      last = key
      await report($, statePath, e.rateLimits)
    }
    return result
  })

  // The engine refuses a compaction under the command's own hook: it starts right after, outside
  // this dispatch. LimitSwitcher's line in the status bar shows it; a toast only says what that
  // line could not (no app running, or no compaction).
  on('command.run', { command: 'jevcompact' }, async $ => {
    $.clock.after(0, async () => {
      const { text, shown } = await compactByHand($, statePath)
      if (!shown) $.ui.toast(text)
    })
    return { text: 'Jev compacting…' }
  })

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'jevcompact',
      description: 'Shrink this session\'s old tool outputs with Jev (needs jev-compact and an OpenRouter key)',
    })
    const result = await next(e)
    void poll($, statePath) // never holds the session's start
    $.clock.every(EVERY, () => poll($, statePath))
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.reason === 'error' && !e.agentId) {
      for (const ms of SOON) $.clock.after(ms, () => poll($, statePath)) // the app may want a compaction now
    }
    return result
  })
}
