// Images in tool outputs (a screenshot Read from disk, an MCP tool's image block). Claude Code hands
// them over in a result's stored record, not in its text, so without this they look empty: never
// asked about, never counted, and a screenshot-heavy session keeps every one it ever looked at.
//
// Sizes are estimates without a tokenizer: an image costs about width × height / 750 tokens, at most
// about 1.15 megapixels' worth (the API scales larger ones down); one without dimensions counts as
// the largest. Measured on a real session: 53 screenshots, 340 to 2,200 tokens each in Claude's count.

import { CHARS_PER_TOKEN } from './tokens.ts'

const MAX_PIXELS = 1_150_000
const MAX_TOKENS = Math.ceil(MAX_PIXELS / 750)

type Loose = Record<string, unknown>
const isObject = (v: unknown): v is Loose => v !== null && typeof v === 'object'
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : undefined)

function tokensFor(dimensions: unknown): number {
  if (!isObject(dimensions)) return MAX_TOKENS
  const w = num(dimensions['displayWidth']) ?? num(dimensions['width']) ?? num(dimensions['originalWidth'])
  const h = num(dimensions['displayHeight']) ?? num(dimensions['height']) ?? num(dimensions['originalHeight'])
  return w && h ? Math.ceil(Math.min(w * h, MAX_PIXELS) / 750) : MAX_TOKENS
}

/** One image record, or null: `{ type: 'image', source: { data } }` (an API block), `{ type: 'image', data }` (MCP), or Read's `{ type: 'image', file: { base64, dimensions } }`. */
function imageTokens(o: Loose): number | null {
  if (o['type'] !== 'image') return null
  const file = isObject(o['file']) ? o['file'] : undefined
  const source = isObject(o['source']) ? o['source'] : undefined
  const data = file?.['base64'] ?? source?.['data'] ?? o['data']
  if (typeof data !== 'string' || data.length === 0) return null
  return tokensFor(file?.['dimensions'] ?? o['dimensions'])
}

/** The estimated tokens of every image in a stored tool record (0 when it holds none). */
export function imageTokensOf(value: unknown, depth = 0): number {
  if (depth > 4 || !isObject(value)) return 0
  if (Array.isArray(value)) return value.reduce((sum: number, item) => sum + imageTokensOf(item, depth + 1), 0)
  const own = imageTokens(value)
  if (own !== null) return own
  let sum = 0
  for (const key of ['content', 'result', 'images', 'image', 'output']) sum += imageTokensOf(value[key], depth + 1)
  return sum
}

/** The same in characters, the unit the rest of the library sizes outputs in. */
export function imageCharsOf(value: unknown): number {
  return Math.round(imageTokensOf(value) * CHARS_PER_TOKEN)
}
