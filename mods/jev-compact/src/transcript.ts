import { imageCharsOf, imageTokensOf } from './images.ts'
import { hasCompactionNote } from './apply.ts'
import type { Message, ToolCall, ToolResult } from './types.ts'

/**
 * Pairs every tool_use with its tool_result by `tool_use_id`, oldest first, labelled `t1`, `t2`, ...
 * A call is pinned when it or its result sits in the first message or among the newest
 * `preserveRecentMessages` messages, or when it has no result yet.
 */
export function collectToolCalls(messages: readonly Message[], preserveRecentMessages: number): ToolCall[] {
  const results = new Map<string, { index: number; result: ToolResult }>()
  messages.forEach((message, index) => {
    for (const result of message.toolResults ?? []) results.set(result.tool_use_id, { index, result })
  })
  const recentFrom = Math.max(1, messages.length - Math.max(0, preserveRecentMessages))
  const calls: ToolCall[] = []
  messages.forEach((message, index) => {
    if (message.role !== 'assistant') return
    for (const use of message.toolUses) {
      const found = results.get(use.tool_use_id)
      const resultText = found?.result.text ?? use.text ?? ''
      const stored = found ? found.result.result : use.result
      calls.push({
        id: `t${calls.length + 1}`,
        tool_use_id: use.tool_use_id,
        tool: use.tool,
        input: use.input ?? {},
        useIndex: index,
        resultIndex: found?.index ?? null,
        resultText,
        resultChars: resultText.length + imageCharsOf(stored),
        imageTokens: imageTokensOf(stored),
        isError: found?.result.isError ?? use.isError ?? false,
        pinned: index === 0 || index >= recentFrom || found === undefined || found.index >= recentFrom,
      })
    }
  })
  return calls
}

/** The characters a pair carries: its input as JSON plus its result text. */
export function pairChars(call: ToolCall): number {
  return JSON.stringify(call.input).length + call.resultChars
}

const WHOLE_READ_KEYS = new Set(['file_path'])

/** The file a Read call read whole (no offset, limit or pages), else null. */
function wholeRead(call: ToolCall): string | null {
  if (call.tool !== 'Read' || call.isError) return null
  const path = call.input['file_path']
  if (typeof path !== 'string' || !path) return null
  return Object.keys(call.input).every((key) => WHOLE_READ_KEYS.has(key)) ? path : null
}

/** Exact duplicate calls mapped to the later canonical tool-use ID. No path-only or line matching. */
export function duplicateSources(calls: readonly ToolCall[]): Map<string, string> {
  const latest = new Map<string, ToolCall>()
  const sources = new Map<string, string>()
  for (const call of [...calls].reverse()) {
    if (call.isError || call.resultIndex === null || call.imageTokens > 0 || hasCompactionNote(call.resultText)) continue
    // Exact inputs and bytes only: a changed revision or a different command is separate evidence.
    const key = JSON.stringify([call.tool, call.input, call.resultText])
    const newer = latest.get(key)
    if (newer && !call.pinned) sources.set(call.id, newer.tool_use_id)
    else if (!newer) latest.set(key, call)
  }
  return sources
}

export function supersededCalls(calls: readonly ToolCall[]): Set<string> {
  const reads = calls.filter((call) => wholeRead(call) !== null)
  return new Set(duplicateSources(reads).keys())
}
