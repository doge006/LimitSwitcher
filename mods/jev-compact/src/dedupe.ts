// Lossless compression within one output. Preserve whitespace, ordering, and the exact
// repetition count. Cross-output deduplication uses exact input/output identity in transcript.ts.
import { hasCompactionNote } from './apply.ts'
export const REPEAT_TAG = '[LimitSwitcher repeat: '

export function repeatText(text: string): string {
  if (hasCompactionNote(text)) return text
  const lines = text.split('\n')
  const out: string[] = []
  for (let i = 0; i < lines.length;) {
    let end = i + 1
    while (end < lines.length && lines[end] === lines[i]) end++
    const count = end - i
    const note = `${REPEAT_TAG}${count - 1} additional copies of the preceding line]`
    if (count >= 4 && count <= 1000001 && lines[i]!.length > 0 && note.length + 1 < (lines[i]!.length + 1) * (count - 1)) {
      out.push(lines[i]!, note)
    } else for (let at = i; at < end; at++) out.push(lines[at]!)
    i = end
  }
  const result = out.join('\n')
  return result.length < text.length ? result : text
}

/** Exact round trip for this module's notation; used by retention tests and offline tools. */
export function expandRepeats(text: string): string {
  const out: string[] = []
  for (const line of text.split('\n')) {
    const match = /^\[LimitSwitcher repeat: (\d+) additional copies of the preceding line\]$/.exec(line)
    if (match && out.length) {
      const count = Number(match[1])
      if (!Number.isSafeInteger(count) || count > 1000000) throw new Error('invalid repeat count')
      const previous = out[out.length - 1]!
      for (let i = 0; i < count; i++) out.push(previous)
    } else out.push(line)
  }
  return out.join('\n')
}
