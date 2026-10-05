// Budget mode, for long sessions: after the usual decisions, keep stepping outputs down (kept whole ->
// head and tail -> a note naming what it held) until the conversation fits a target share of its
// size. The least needed go first, and an older output counts as less needed than a newer one with
// the same score: what a session did hours ago is the most likely to be finished with. Nothing the
// usual decisions protect is touched (pinned, errors, superseded reads, small outputs).

import type { CompactOptions, Decision, ToolCall } from './types.ts'

// Long sessions get it by themselves: above this estimated conversation size, keep this share. Measured
// on a 731k-token session (smaller / facts lost): default 58% / 16; keep 40% 61% / 20; keep 35% 63% / 25;
// keep 30% 64% / 36 (the rest is protected: text, the newest messages, errors, the notes themselves).
export const LONG_SESSION_TOKENS = 333_000
export const LONG_TARGET = 0.35
export const AGE_WEIGHT = 0.2   // the oldest output counts as this much less needed than the newest
const NOTE_CHARS = 250          // a note's usual length, its index aside

/** Steps decisions down in place until the estimated size fits; returns how many steps it took. */
export function tighten(calls: readonly ToolCall[], decisions: Decision[], options: CompactOptions, before: number, now: number): number {
  const target = before * options.targetRatio
  if (now <= target) return 0
  const position = new Map(calls.map((call, i) => [call.id, i] as const))
  const byId = new Map(calls.map((call) => [call.id, call] as const))
  const steppable = decisions.filter((d) => (d.action === 'keep' || d.action === 'trim') && d.need !== undefined)
  const score = (d: Decision) => d.need! - AGE_WEIGHT * (1 - position.get(d.id)! / Math.max(1, calls.length - 1))
  steppable.sort((a, b) => score(a) - score(b))
  const window = options.trimHeadChars + options.trimTailChars
  let steps = 0
  for (let pass = 0; pass < 2 && now > target; pass += 1) {
    for (const d of steppable) {
      if (now <= target) break
      const call = byId.get(d.id)!
      if (d.action === 'keep' && call.resultChars > window + NOTE_CHARS) {
        now -= call.resultChars - window - NOTE_CHARS
        d.action = 'trim'
        steps += 1
      } else if (d.action === 'trim' && pass > 0) {
        now -= Math.max(0, Math.min(call.resultChars, window + NOTE_CHARS) - NOTE_CHARS)
        d.action = 'stub'
        steps += 1
      }
    }
  }
  return steps
}
