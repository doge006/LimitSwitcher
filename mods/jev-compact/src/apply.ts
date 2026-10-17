// Rebuilds the transcript from the decisions. Nothing is summarised and nothing is removed whole:
// outputs may be shortened, with explicit omission notes. Inputs remain verbatim: current disk
// contents cannot recover historical scripts or edits. No archive retrieval is enabled.

import { factIndex, factsIn, indexBudget } from './facts.ts'
import { imageTokensOf } from './images.ts'
import type { Decision, Message, ToolCall, ToolResult, ToolUse } from './types.ts'

export const NOTE_TAG = '[LimitSwitcher'
const OMISSION_GUIDANCE = 'Omitted details are unavailable from this note. Consult retained original evidence if available; do not repeat state-changing actions to reconstruct it.'

/** Only emitted note lines, not source code that happens to mention the product's marker. */
export function hasCompactionNote(text: string): boolean {
  return /(?:^|\n)\[LimitSwitcher(?::| (?:removed |duplicate of tool_use_id=|repeat: ))/.test(text)
}

/** " It held: a, b, c." for a note, or "" when there is nothing worth naming. */
function held(names: readonly string[]): string {
  return names.length ? ` It held: ${names.join(', ')}.` : ''
}

export function stubText(call: ToolCall, superseded: boolean, index: readonly string[] = [], source?: string): string {
  if (superseded && source) return `${NOTE_TAG} duplicate of tool_use_id=${JSON.stringify(source)}; identical input and output. Read that retained result for details.]`
  const why = 'judged not needed'
  const what = call.imageTokens > 0
    ? `${call.tool} output (${call.resultText.length ? `${call.resultText.length} chars and ` : ''}an image, ~${call.imageTokens} tokens)`
    : `${call.tool} output (${call.resultChars} chars)`
  return `${NOTE_TAG} removed ${what}, ${why}.${held(index)} ${OMISSION_GUIDANCE}]`
}

/** The middle of `text` replaced by a note (naming what it held), or `text` when there is nothing to cut. */
export function trimText(text: string, head: number, tail: number, known: ReadonlySet<string> = new Set(), visible: ReadonlySet<string> = new Set()): string {
  if (text.length <= head + tail) return text
  const middle = text.slice(head, text.length - tail)
  const omitted = middle.length
  return `${text.slice(0, head)}\n\n${NOTE_TAG} removed ${omitted} chars from the middle of this output before an account swap.${held(factIndex(middle, known, indexBudget(middle.length), visible))} ${OMISSION_GUIDANCE}]\n\n${tail > 0 ? text.slice(-tail) : ''}`
}

type Edit = { result?: string; input?: Record<string, unknown> }

/** What each tool_use_id's call becomes. */
export function editsFor(
  calls: readonly ToolCall[],
  decisions: readonly Decision[],
  options: { trimHeadChars: number; trimTailChars: number; maxWriteInputChars: number },
  visible: ReadonlySet<string> = new Set(),
): Map<string, Edit> {
  const byId = new Map(calls.map((call) => [call.id, call] as const))
  const edits = new Map<string, Edit>()
  // What the assistant itself worked with (its tool inputs): those names come first in a note's index
  const known = factsIn(calls.map((call) => JSON.stringify(call.input)).join('\n'))
  // Newest first: a name a newer note already gives is not repeated in an older one
  const named = new Set(visible)
  const index = (text: string) => {
    const picked = factIndex(text, known, indexBudget(text.length), named)
    for (const fact of picked) named.add(fact)
    return picked
  }
  for (const decision of [...decisions].reverse()) {
    const call = byId.get(decision.id)
    if (!call || decision.action === 'pinned') continue
    const edit: Edit = {}
    if (decision.action === 'stub' || decision.action === 'superseded') {
      edit.result = stubText(call, decision.action === 'superseded', decision.source ? [] : index(call.resultText), decision.source)
    } else if (decision.action === 'trim') {
      const text = trimText(call.resultText, options.trimHeadChars, options.trimTailChars, known, named)
      if (text !== call.resultText) {
        const middle = call.resultText.slice(options.trimHeadChars, call.resultText.length - options.trimTailChars)
        for (const fact of factIndex(middle, known, indexBudget(middle.length), named)) named.add(fact)
      }
      if (text !== call.resultText) edit.result = text
    }
    // Inputs may be the only copy of historical values or scripts. A current file is not
    // proof of recoverability, so preserve every input until verified archival retrieval exists.
    if (edit.result !== undefined && edit.result.length >= call.resultChars) delete edit.result
    if (edit.result !== undefined || edit.input !== undefined) edits.set(call.tool_use_id, edit)
  }
  return edits
}

/**
 * The transcript with the edits applied. A message nothing touched is the same object (its engine
 * handle included, so the engine keeps it whole); a changed one is rebuilt from its role, text and
 * tool blocks. The set of calls and results, and their order, never changes.
 *
 * The engine builds a rebuilt message's tool results from their text alone, so an image kept beside an
 * output that changed would go with it: a message holding a kept image stays whole instead.
 */
export function applyEdits(messages: readonly Message[], edits: ReadonlyMap<string, Edit>): Message[] {
  if (edits.size === 0) return [...messages]
  return messages.map((message) => {
    if (message.toolResults?.some((r) => !edits.get(r.tool_use_id)?.result && imageTokensOf(r.result) > 0)) return message
    let changed = false
    const toolUses: ToolUse[] = message.toolUses.map((use) => {
      const input = edits.get(use.tool_use_id)?.input
      if (!input) return use
      changed = true
      return { ...use, input }
    })
    const toolResults: ToolResult[] | undefined = message.toolResults?.map((result) => {
      const text = edits.get(result.tool_use_id)?.result
      if (text === undefined) return result
      changed = true
      return { tool_use_id: result.tool_use_id, text, isError: result.isError }
    })
    if (!changed) return message
    const rebuilt: Message = { role: message.role, text: message.text, toolUses }
    if (toolResults && toolResults.length > 0) rebuilt.toolResults = toolResults
    return rebuilt
  })
}
