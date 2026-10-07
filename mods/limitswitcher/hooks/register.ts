import { atom, read, update } from 'claude-code'
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
// `/swapaccount <name or email>` moves this window, and only this one, to another account: the app's
// router puts that account's login on the window's requests. A window that isn't going through the
// router yet is pointed at it here, in place (ANTHROPIC_BASE_URL, read again for each request), and
// joins the app's Windows list; everything else stays this Claude Code window as it was.
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

// What `/resume` says as its list opens, while Settings → Jev compaction is on
export const RESUME_TOAST = '💡 Jev compaction is on: upon resuming, Jev will compact the context, saving usage.'
export const COMPACTING = '⇄ LimitSwitcher · Jev compacting the resumed session…'
const NOTE_FOR = 60_000 // the note stays up while a session is picked and "Resume this conversation?" is answered (a toast's longest)
const SAVED_FOR = 15_000 // the toast says what Jev saved for this long
const ANSWERED_AFTER = 1_500 // "Start a new conversation" ends the session within this (its /clear)

// The band above the prompt while a resumed session is compacted: a toast can't be taken down
// once shown (they stack), so "compacting" is a band, gone when it is done.
const band = atom({ plugin: 'limitswitcher', key: 'band' } as const, null)
function toast($: any, text: string, timeoutMs?: number): void {
  $.ui.toast(text, timeoutMs ? { timeoutMs } : undefined)
}

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
    const window = await $.env.get('LIMITSWITCHER_WINDOW')
    const answer = await post($, app, '/api/statusline', {
      rate_limits: Object.keys(limits).length ? limits : null, session, source: 'mod',
      ...(window ? { window: String(window) } : {}),
    })
    const on = answer?.jevResume === true
    jevResume = on
    for (const alert of Array.isArray(answer?.alerts) ? answer.alerts : []) {
      // Reset alerts (Settings): every account of a provider had hit its limit and one has room again
      if (typeof alert?.id !== 'string' || typeof alert?.text !== 'string' || toasted.has(alert.id)) continue
      toasted.add(alert.id)
      toast($, alert.text)
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

/** Whether a SessionStart is a cold resume worth compacting first: an old conversation loaded
 * (never an account switch, which goes on in the same session) whose cache has expired, with
 * enough context that reloading it costs a real share. */
export function coldResume(e: Resume): boolean {
  return e.source === 'resume' && !e.agent_id
    && e.prompt_cache_likely_expired === true && (e.context_tokens ?? 0) >= RESUME_MIN
}

/** What the toast says after a compaction on resume: tokens saved, of how many, and the share. */
export function resumeSavedText(saved: number, before: number, context?: number): string {
  const share = before > 0 ? Math.round((saved / before) * 100) : 0
  const of = context && context > saved ? ` of ${Math.round(context / 1000)}k` : ''
  return `✅ Jev saved ~${Math.round(saved / 1000)}k${of} tokens (${share}%) before this resume`
}

/** The compaction on resume: asks the app first (it says whether Jev compaction is on, and shows
 * "Jev Compacting…" in its line), compacts, tells the app how it went, and toasts what it saved.
 * An app from before this answers without `resume` and nothing happens. */
type Agreed = { app: App; session: string; id: string }

/** Asks the app whether to compact a resumed session now (Settings → Jev compaction on; its line
 * then shows "Jev Compacting…"). An app from before this answers without `resume`: no. */
async function agreeOnResume($: any, statePath: string, e: Resume): Promise<Agreed | null> {
  const app = await appOf($, statePath)
  if (!app) return null
  const session = e.session_id
  const id = `resume-${Date.now().toString(36)}`
  try {
    const answer = await post($, app, '/api/compaction', { session, id, outcome: 'running', resume: true })
    return answer?.ok === true && answer?.resume === true ? { app, session, id } : null // off, too old, or a swap's is under way
  } catch {
    return null
  }
}

async function compactOnResume($: any, e: Resume, { app, session, id }: Agreed): Promise<void> {
  await update($, band, () => COMPACTING)
  let outcome = 'skipped'
  let saved = 0
  let skip: string | undefined
  for (let busy = 1; ; busy++) {
    try {
      started = true
      const result = await $.session.compact({ instructions: MARKER })
      skip = result.skip
      if (skip === undefined) {
        const before = typeof result.tokensBefore === 'number' ? result.tokensBefore : 0
        saved = typeof result.tokensAfter === 'number' ? Math.max(0, Math.round(before - result.tokensAfter)) : 0
        outcome = 'done'
        if (saved > 0) {
          toast($, resumeSavedText(saved, before, e.context_tokens), SAVED_FOR)
        }
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
  await update($, band, () => null)
  try {
    await post($, app, '/api/compaction', { session, id, outcome, saved, ...(skip ? { reason: skip.slice(0, 300) } : {}) })
  } catch {
    // the app stops showing it on its own
  }
}

let resuming: Promise<void> | null = null // the compaction on resume, while it runs
let started = false // the compaction itself has started: Claude Code queues a message sent now behind it
let pending: Resume | null = null // an old conversation loaded: compacted once "Resume this conversation?" is answered
let armed = false // the prompt area was drawn while one is pending: the compaction starts shortly unless it is dropped

/** Runs the compaction the app agreed to, outside the dispatch that started it. */
function startCompaction($: any, e: Resume, agreed: Agreed): void {
  started = false
  const running: Promise<void> = new Promise<void>((resolve) => {
    $.clock.after(0, () => compactOnResume($, e, agreed).catch(() => {}).finally(resolve))
    $.clock.after(RESUME_LONGEST, resolve)
  }).then(() => {
    if (resuming !== running) return
    resuming = null
  })
  resuming = running
}

/** `/swapaccount <name or email>`: this window on another account (see the top of this file). */
async function swapAccount($: any, statePath: string, account: string): Promise<string> {
  const app = await appOf($, statePath)
  if (!app) return 'LimitSwitcher isn\'t running.'
  try {
    const window = await $.env.get('LIMITSWITCHER_WINDOW')
    const answer = await post($, app, '/api/swapaccount', {
      session: String(await $.session.id()),
      account,
      cwd: await $.session.cwd(),
      ...(window ? { window: String(window) } : {}),
    })
    if (answer === null) return 'This LimitSwitcher is too old for /swapaccount: update it (Settings → Update).'
    if (typeof answer.window === 'string' && typeof answer.baseUrl === 'string' && answer.baseUrl.startsWith('http://127.0.0.1:')) {
      // from the next request on, this window goes through the router (and its hooks, status line and
      // tools, started from now on, know which window they are in)
      await $.env.set('ANTHROPIC_BASE_URL', answer.baseUrl)
      await $.env.set('LIMITSWITCHER_WINDOW', answer.window)
    }
    return typeof answer.text === 'string' && answer.text ? answer.text : 'LimitSwitcher didn\'t say.'
  } catch {
    return 'LimitSwitcher didn\'t answer; try again in a moment.'
  }
}

/** `/limits`: every Claude account at a glance, as the app has them (for the phone over Remote
 * Control, where the status line doesn't show). Answered here, so it costs no model turn. With
 * separate accounts per window, the window's id (LIMITSWITCHER_WINDOW) says which account it is on. */
async function limitsText($: any, statePath: string): Promise<string> {
  const app = await appOf($, statePath)
  if (!app) return 'LimitSwitcher isn\'t running.'
  try {
    const { context } = await $.session.usage()
    const window = await $.env.get('LIMITSWITCHER_WINDOW')
    const answer = await post($, app, '/api/limits', {
      session: String(await $.session.id()),
      model: await $.session.model(),
      context: context ? { tokens: context.tokens, window: context.window, percent: context.percent } : null,
      ...(window ? { window: String(window) } : {}),
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

  // An old conversation loaded: nothing yet. Claude Code may still be asking "Resume this
  // conversation?", and "Start a new conversation" there drops it (a /clear, so session.end).
  // Claude Code draws no prompt area while it asks: the first drawing after the load means it
  // was answered (or never asked), and the compaction starts then, before anything is typed.
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    pending = statePath && coldResume(e) ? e : null
    armed = false
    if (pending && jevResume) toast($, RESUME_TOAST, NOTE_FOR) // beside the question (loading cleared /resume's)
    return result
  })

  on('session.end', async ($, e, next) => {
    pending = null // a new conversation (the question's other answer), or the window closed
    return next(e)
  })

  // A message sent while Jev compacts the resumed session is queued by Claude Code behind the
  // compaction and goes, as typed, once it is done (this hook sees it then). One sent in the
  // moment before the compaction starts goes back into the prompt box, to send again: its usage
  // is what the question warned of. (A hook can't hold it that long, and sent again by the mod it
  // would read "Prompt from the limitswitcher plugin".)
  on('prompt.submit', async ($, e, next) => {
    const kind = e.origin?.kind ?? 'composer' // absent: the person's own
    if (kind !== 'composer' && kind !== 'bridge') return next(e)
    if (pending && !resuming && jevResume) { // a message sent before it started: start it now
      const resumed = pending
      pending = null
      const agreed = await agreeOnResume($, statePath, resumed)
      if (agreed) startCompaction($, resumed, agreed)
    }
    if (!resuming || started) return next(e)
    const text = e.text
    $.clock.after(0, () => $.prompt.fill({ text }).catch(() => {})) // once the box has been cleared
    return {
      drop: e.attachments?.length
        ? 'Jev is starting to compact this resumed session: your message is back in the box (paste the image again). Send it again.'
        : 'Jev is starting to compact this resumed session: your message is back in the box. Send it again.',
    }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (pending && !armed && !resuming) {
      armed = true
      const resumed = pending
      $.clock.after(ANSWERED_AFTER, () => {
        if (pending !== resumed || resuming || !jevResume) return // a new conversation, or already started
        pending = null
        void agreeOnResume($, statePath, resumed).then((agreed) => { if (agreed && !resuming) startCompaction($, resumed, agreed) })
      })
    }
    const text = await read($, band)
    if (text === null || e.props.hasSurvey) return next(e)
    const { Text } = $.ui.resolve(e)
    return h(Text, { color: 'claude', bold: true }, text)
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
      if (!shown) toast($, text)
    })
    return { text: 'Jev compacting…' }
  })

  // `/resume` says Jev will compact an old session as its list of sessions opens (Claude Code's own
  // "Resume this conversation?" can't carry a mod's note)
  on('command.run', { command: 'resume' }, async ($, e, next) => {
    if (jevResume) toast($, RESUME_TOAST, NOTE_FOR) // up while a session is picked (a click takes it off)
    return next(e)
  })

  on('command.run', { command: 'limits' }, async $ => ({ text: await limitsText($, statePath) }))

  on('command.run', { command: 'swapaccount' }, async ($, e) => ({ text: await swapAccount($, statePath, String(e.args || '').trim()) }))

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'limits',
      description: 'Every Claude account\'s usage limits at a glance (LimitSwitcher)',
    })
    await $.command.register({
      name: 'swapaccount',
      description: 'Move this window (only this one) to another Claude account (LimitSwitcher)',
      argumentHint: '<name or email>',
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
