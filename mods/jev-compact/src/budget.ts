// A size target is an observation, never permission to erase needed evidence.
// The caller reports a missed target instead of downgrading keep/trim decisions.
export const LONG_SESSION_TOKENS = 333_000
export const LONG_TARGET = 0.35

export function targetRatio(requested: number, tokensBefore: number): number {
  return requested < 0 ? (tokensBefore > LONG_SESSION_TOKENS ? LONG_TARGET : 0) : requested
}
