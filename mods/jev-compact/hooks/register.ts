import type { EngineInterface, Register, SessionMessage } from 'claude-code'

import { compact, reductionRatio, summarize } from '../src/compact.ts'
import { askerOver, type Transport } from '../src/openrouter.ts'

// Shrinks the tool outputs a session no longer needs, scored by Jev, when LimitSwitcher asks for
// it right before it swaps the session to another account: the new account has none of the
// session cached, so every token it does not have to load is usage saved. It answers the
// compaction itself, so Claude Code makes no model request (it works on an account at 0%).
//
// It acts only on LimitSwitcher's own request (`$.session.compact` from the limit-status mod with
// MARKER as its instructions, also for `/jevcompact` typed by hand); plain /compact and
// auto-compaction stay Claude Code's own. The request is
// never handed to Claude Code's summary: when this can't prune, it skips, and limit-status retries
// or lets the session go on without it.

export const MARKER = 'limitswitcher:jev-compact'
// A skip LimitSwitcher should retry starts with this (Jev or the network failed); any other skip
// (no key, nothing worth pruning) is final.
export const FAILED = 'Jev failed: '
const MIN_REDUCTION = 0.03 // less than this is not worth a compaction boundary in the transcript

/** The OpenRouter key: the environment, then settings.json's env block, then the LimitSwitcher .env file. */
async function apiKeyOf($: EngineInterface, envFile: string): Promise<string | undefined> {
  const fromEnv = await $.env.get('OPENROUTER_API_KEY')
  if (fromEnv) return fromEnv.trim()
  try {
    const block = (await $.settings.read())['env']
    const value = block && typeof block === 'object' ? (block as Record<string, unknown>)['OPENROUTER_API_KEY'] : undefined
    if (typeof value === 'string' && value.trim()) return value.trim()
  } catch {
    // no settings to read
  }
  if (!envFile) return undefined
  try {
    return keyInEnvFile(await $.fs.read(envFile))
  } catch {
    return undefined // no file yet
  }
}

/** The value of an `OPENROUTER_API_KEY=` line (optionally `export`ed and quoted). */
export function keyInEnvFile(text: string): string | undefined {
  for (const line of text.split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*)$/.exec(line)
    if (!match) continue
    const value = match[1]!.trim().replace(/^(['"])(.*)\1$/, '$2').trim()
    if (value) return value
  }
  return undefined
}

function transportOf($: EngineInterface): Transport {
  return async (url, init) => {
    const response = await $.http.fetch(url, init)
    return { status: response.status, ok: response.ok, text: response.text }
  }
}


export const register: Register = (on, options) => {
  const envFile = String(options.envFile ?? '')

  on('session.compact', async ($, e, next) => {
    // LimitSwitcher's own request (`/jevcompact` is that request made by hand), or `/compact limitswitcher:jev-compact`
    const ours = (e.trigger === 'plugin' || e.trigger === 'manual') && e.instructions?.trim() === MARKER
    if (!ours || e.agentId !== undefined) return next(e)
    let reason: string
    const key = await apiKeyOf($, envFile)
    if (!key) {
      reason = 'no OpenRouter key (OPENROUTER_API_KEY)'
    } else {
      try {
        const result = await compact(e.messages, askerOver(transportOf($), { apiKey: key }))
        const ratio = reductionRatio(result)
        const summary = summarize(result)
        if (ratio >= MIN_REDUCTION) {
          // LimitSwitcher's line in the status bar says what it saved; the details are for debugging
          await $.ui.log(`Jev compaction: pruned, nothing summarised: ${summary}`, { to: 'debug' })
          return {
            messages: result.messages as SessionMessage[],
            tokensBefore: result.stats.tokensBefore,
            tokensAfter: result.stats.tokensAfter,
          }
        }
        reason = `nothing worth pruning (${summary})`
      } catch (error) {
        reason = FAILED + (error instanceof Error ? error.message : String(error))
      }
    }
    await $.ui.log(`Jev compaction skipped: ${reason}`, { to: 'debug' })
    return { skip: reason }
  })
}
