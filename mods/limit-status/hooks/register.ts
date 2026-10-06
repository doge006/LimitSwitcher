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
//
// `/limits` shows every Claude account's limits at a glance, from the app's numbers (the status
// line doesn't show on the phone over Remote Control).
//
// When an old session is resumed (Claude Code's "Resume this conversation?" said it costs a share
// of the usage limit: none of it is cached any more), it has Jev compact the session before the
// first message goes, holds that message meanwhile, then says how much less there is to load. The
// "Resume?" question itself is Claude Code's own and can't be changed by a mod: this starts right
// after it is answered. It follows Settings → Jev compaction.
//
// And it shows the app's reset alerts as a toast: when every account of a provider had hit its
// limit and one has room again, each open session hears of it on its next report.
const EVERY = 30_000 // an idle session still reports now and then, and picks up a compaction asked for
const SOON = [2_000, 5_000, 10_000, 20_000, 40_000] // after a turn that ended on an error (a usage limit): look sooner

// Shared with mods/jev-compact/hooks/register.ts (plugins can't import each other).
const MARKER = 'limitswitcher:jev-compact'
const FAILED = 'Jev failed: '

const TRIES = 3          // Jev failed: wait, try again, wait, one more time, then give up
const RETRY_AFTER = 30_000
const BUSY_TRIES = 6     // a turn is running (the session went on, or is just ending): look again shortly
const BUSY_AFTER = 5_000

const RESUME_MIN = 100_000  // context tokens: a resume smaller than this costs too little to compact first
const RESUME_LONGEST = 240_000 // never hold the first message longer than this

type Window = { kind: string; percentUsed: number; resetsAt?: string }
type App = { base: string; token: string }

// What `/resume` says before a session is picked, while Settings → Jev compaction is on
export const RESUME_NOTE = 'Jev compacts an old session before it goes on (LimitSwitcher)'
export const RESUME_TOAST = '⇄ LimitSwitcher · Jev compaction is on: resuming an old session compacts it first, then says what it saved'

let jevResume = false // the app's last word on it (each report)
const handled = new Set<string>() // compaction ids already run (the app keeps asking until it hears back)
const toasted = new Set<string>() // reset alerts already shown in this session (the app offers each for a few minutes)

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
    const on = answer?.jevResume === true
    if (on !== jevResume) {
      jevResume = on
      $.ui.invalidate('command.describe') // /resume's line in the list follows it
    }
    for (const alert of Array.isArray(answer?.alerts) ? answer.alerts : []) {
      // Reset alerts (Settings): every account of a provider had hit its limit and one has room again
      if (typeof alert?.id !== 'string' || typeof alert?.text !== 'string' || toasted.has(alert.id)) continue
      toasted.add(alert.id)
      $.ui.toast(alert.text)
    }
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

type Resume = { session_id: string; source: string; agent_id?: string; context_tokens?: number; prompt_cache_likely_expired?: boolean }

/** Whether a SessionStart is a cold resume worth compacting first: a resumed (or forked) main
 * session whose cache has expired, with enough context that reloading it costs a real share. */
export function coldResume(e: Resume): boolean {
  return (e.source === 'resume' || e.source === 'fork') && !e.agent_id
    && e.prompt_cache_likely_expired === true && (e.context_tokens ?? 0) >= RESUME_MIN
}

/** What the toast says after a compaction on resume: tokens saved, of how many, and the share. */
export function resumeSavedText(saved: number, before: number, context?: number): string {
  const share = before > 0 ? Math.round((saved / before) * 100) : 0
  const of = context && context > saved ? ` of ${Math.round(context / 1000)}k` : ''
  return `⇄ LimitSwitcher · Jev saved ~${Math.round(saved / 1000)}k${of} tokens (${share}%) before this resume`
}

/** The compaction on resume: asks the app first (it says whether Jev compaction is on, and shows
 * "Jev Compacting…" in its line), compacts, tells the app how it went, and toasts what it saved.
 * An app from before this answers without `resume` and nothing happens. */
async function compactOnResume($: any, statePath: string, e: Resume): Promise<void> {
  const app = await appOf($, statePath)
  if (!app) return
  const session = e.session_id
  const id = `resume-${Date.now().toString(36)}`
  try {
    const answer = await post($, app, '/api/compaction', { session, id, outcome: 'running', resume: true })
    if (answer?.ok !== true || answer?.resume !== true) return // off, too old, or a swap's compaction is under way
  } catch {
    return
  }
  let outcome = 'skipped'
  let saved = 0
  let skip: string | undefined
  for (let busy = 1; ; busy++) {
    try {
      const result = await $.session.compact({ instructions: MARKER })
      skip = result.skip
      if (skip === undefined) {
        const before = typeof result.tokensBefore === 'number' ? result.tokensBefore : 0
        saved = typeof result.tokensAfter === 'number' ? Math.max(0, Math.round(before - result.tokensAfter)) : 0
        outcome = 'done'
        if (saved > 0) $.ui.toast(resumeSavedText(saved, before, e.context_tokens))
      } else {
        outcome = skip.startsWith(FAILED) ? 'failed' : 'skipped'
      }
      break
    } catch (error) {
      if (busy < BUSY_TRIES) {
        await new Promise<void>((resolve) => $.clock.after(BUSY_AFTER, resolve))
        continue
      }
      skip = `the session is busy (${error instanceof Error ? error.message : String(error)})`
      break
    }
  }
  try {
    await post($, app, '/api/compaction', { session, id, outcome, saved, ...(skip ? { reason: skip.slice(0, 300) } : {}) })
  } catch {
    // the app stops showing it on its own
  }
}

/** `/limits`: every Claude account at a glance, as the app has them (for the phone over Remote
 * Control, where the status line doesn't show). Answered here, so it costs no model turn. The
 * window's own folder says which account it is on, with separate accounts per window. */
async function limitsText($: any, statePath: string): Promise<string> {
  const app = await appOf($, statePath)
  if (!app) return 'LimitSwitcher isn\'t running.'
  try {
    const { context } = await $.session.usage()
    const configDir = await $.env.get('CLAUDE_CONFIG_DIR')
    const answer = await post($, app, '/api/limits', {
      session: String(await $.session.id()),
      model: await $.session.model(),
      context: context ? { tokens: context.tokens, window: context.window, percent: context.percent } : null,
      ...(configDir ? { configDir: String(configDir) } : {}),
    })
    if (answer === null) return 'This LimitSwitcher is too old for /limits: update it (Settings → Update).'
    return typeof answer.text === 'string' && answer.text ? answer.text : 'LimitSwitcher has no limits to show yet.'
  } catch {
    return 'LimitSwitcher didn\'t answer; try again in a moment.'
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
      text = `Jev saved ~${Math.round(saved / 1000)}k tokens`
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
  let resuming: Promise<void> | null = null // the compaction on resume, while it runs
  let held: string | null = null // the first message, held until it is done (sent then)

  // Right after "Resume this conversation?" is answered (Resume): compact first, outside this dispatch
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    if (statePath && !resuming && coldResume(e)) {
      const running: Promise<void> = new Promise<void>((resolve) => {
        $.clock.after(0, () => compactOnResume($, statePath, e).catch(() => {}).finally(resolve))
        $.clock.after(RESUME_LONGEST, resolve)
      }).then(async () => {
        if (resuming !== running) return
        resuming = null
        const text = held
        held = null
        if (text !== null) await $.prompt.submit({ text, asUser: true })
      })
      resuming = running
    }
    return result
  })

  // The person's first message while it runs: held (a hook can't wait that long), then sent as
  // typed once the session is compacted. One with an image can't be sent again by a plugin.
  on('prompt.submit', async ($, e, next) => {
    const kind = e.origin?.kind ?? 'composer' // absent: the person's own
    if (!resuming || (kind !== 'composer' && kind !== 'bridge')) return next(e)
    if (e.attachments?.length) {
      return { drop: 'Jev is compacting this resumed session first; send it again when it says what it saved.' }
    }
    held = held === null ? e.text : `${held}\n\n${e.text}`
    return { drop: 'Jev is compacting this resumed session first; your message goes as soon as it is done.' }
  })

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

  // `/resume` says Jev will compact an old session, in the command list and as it opens the list of
  // sessions (Claude Code's own "Resume this conversation?" can't carry a mod's note)
  on('command.describe', { command: 'resume' }, async ($, e, next) => {
    const result = await next(e)
    return jevResume ? { ...result, description: `${result.description} · ${RESUME_NOTE}` } : result
  })

  on('command.run', { command: 'resume' }, async ($, e, next) => {
    if (jevResume) $.ui.toast(RESUME_TOAST)
    return next(e)
  })

  on('command.run', { command: 'limits' }, async $ => ({ text: await limitsText($, statePath) }))

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'limits',
      description: 'Every Claude account\'s usage limits at a glance (LimitSwitcher)',
    })
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
