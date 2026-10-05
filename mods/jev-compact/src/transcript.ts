import { imageCharsOf, imageTokensOf } from './images.ts'
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

/**
 * The calls whose output a later call makes redundant without asking anyone: a Read of a file
 * that is read whole again later (the later read has the newer content, and it stays). Only
 * whole reads count on both sides, so a part of a file is never taken as standing for all of it.
 */
export function supersededCalls(calls: readonly ToolCall[]): Set<string> {
  const lastRead = new Map<string, number>()
  calls.forEach((call, index) => {
    const path = wholeRead(call)
    if (path !== null) lastRead.set(path, index)
  })
  const superseded = new Set<string>()
  calls.forEach((call, index) => {
    const path = wholeRead(call)
    if (path !== null && (lastRead.get(path) ?? index) > index) superseded.add(call.id)
  })
  return superseded
}
