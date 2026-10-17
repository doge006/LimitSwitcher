// One Jev compaction: pin what must never change, stub what a later call superseded, ask Jev
// about the rest in as few requests as fit, and rebuild the transcript from the answers.
//
// The flow (state fitting, batching) follows cc-mod-jev (MIT, Jay W); the decisions differ: see
// DecisionAction in types.ts and THIRD-PARTY-NOTICES.txt.

import { applyEdits, editsFor, hasCompactionNote, NOTE_TAG } from './apply.ts'
import { factsIn } from './facts.ts'
import { targetRatio } from './budget.ts'
import { repeatText } from './dedupe.ts'
import { noulOf, questionFor, questionName } from './questions.ts'
import { fitState, goalOf } from './state.ts'
import { estimateTokens, transcriptChars } from './tokens.ts'
import { collectToolCalls, pairChars, duplicateSources } from './transcript.ts'
import type { CompactOptions, CompactResult, CompactStats, Decision, JevAnswer, JevAsker, JevQuestions, Message, ToolCall } from './types.ts'

export const DEFAULT_OPTIONS: CompactOptions = {
  keepThreshold: 0.55,
  stubThreshold: 0.22,
  cheapKeepThreshold: 0.55,
  cheapStubThreshold: 0.22,
  preserveRecentMessages: 8,
  minPairChars: 400,
  trimHeadChars: 900,
  trimTailChars: 400,
  maxWriteInputChars: 600,
  maxStateTokens: 20000,
  maxRequestTokens: 28000,
  dedupe: true,
  targetRatio: -1,
}

const REQUEST_OVERHEAD_TOKENS = 200

function finite(value: unknown, fallback: number, min = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(min, value) : fallback
}

export function resolveOptions(options: Partial<CompactOptions> = {}): CompactOptions {
  const d = DEFAULT_OPTIONS
  const keepThreshold = Math.min(1, finite(options.keepThreshold, d.keepThreshold))
  const resolved: CompactOptions = {
    keepThreshold,
    stubThreshold: Math.min(keepThreshold, finite(options.stubThreshold, d.stubThreshold)),
    cheapKeepThreshold: Math.min(1, finite(options.cheapKeepThreshold, d.cheapKeepThreshold)),
    cheapStubThreshold: Math.min(1, finite(options.cheapStubThreshold, d.cheapStubThreshold)),
    preserveRecentMessages: Math.floor(finite(options.preserveRecentMessages, d.preserveRecentMessages)),
    minPairChars: finite(options.minPairChars, d.minPairChars),
    trimHeadChars: Math.floor(finite(options.trimHeadChars, d.trimHeadChars)),
    trimTailChars: Math.floor(finite(options.trimTailChars, d.trimTailChars)),
    maxWriteInputChars: Math.floor(finite(options.maxWriteInputChars, d.maxWriteInputChars, 200)),
    maxStateTokens: finite(options.maxStateTokens, d.maxStateTokens, 1),
    maxRequestTokens: finite(options.maxRequestTokens, d.maxRequestTokens, 1),
    dedupe: options.dedupe ?? d.dedupe,
    targetRatio: Math.min(1, typeof options.targetRatio === 'number' && Number.isFinite(options.targetRatio) ? options.targetRatio : d.targetRatio),
  }
  if (options.goal) resolved.goal = options.goal
  return resolved
}

/** The candidates in batches whose questions, beside the whole state, fit one request; throws when one alone does not. */
export function batchCalls(calls: readonly ToolCall[], stateTokens: number, maxRequestTokens: number): ToolCall[][] {
  const budget = maxRequestTokens - stateTokens - REQUEST_OVERHEAD_TOKENS
  const batches: ToolCall[][] = []
  let current: ToolCall[] = []
  let used = 0
  for (const call of calls) {
    const tokens = estimateTokens(JSON.stringify(questionFor(call)))
    if (tokens > budget) throw new Error(`the question for ${call.id} does not fit beside the state within ${maxRequestTokens} tokens`)
    if (current.length > 0 && used + tokens > budget) {
      batches.push(current)
      current = []
      used = 0
    }
    current.push(call)
    used += tokens
  }
  if (current.length > 0) batches.push(current)
  return batches
}

/** At most three requests in flight; failures prevent queued work from starting. */
export async function mapBatches<T, R>(items: readonly T[], run: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length)
  let next = 0
  let failed = false
  const worker = async () => {
    while (!failed && next < items.length) {
      const index = next++
      try { results[index] = await run(items[index]!) }
      catch (error) { failed = true; throw error }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, items.length) }, worker))
  return results
}

/**
 * The action from Jev's relevance score. Rerun cost does not establish recoverability.
 * A trim that would cut nothing keeps the output whole, and so does one of an image.
 */
export function decide(call: ToolCall, need: number, options: CompactOptions): Decision {
  const base = { id: call.id, tool: call.tool, need, chars: pairChars(call) }
  if (need >= options.keepThreshold) return { ...base, action: 'keep' }
  if (need >= options.stubThreshold) {
    // An image can't be cut to a head and tail: kept whole while Jev is unsure
    const fits = call.imageTokens > 0 || call.resultChars <= options.trimHeadChars + options.trimTailChars
    return { ...base, action: fits ? 'keep' : 'trim' }
  }
  return { ...base, action: 'stub' }
}

function newStats(messages: readonly Message[], calls: readonly ToolCall[]): CompactStats {
  const chars = transcriptChars(messages)
  const tokens = Math.ceil(chars / 3.2)
  return {
    messagesBefore: messages.length, messagesAfter: messages.length, charsBefore: chars, charsAfter: chars,
    tokensBefore: tokens, tokensAfter: tokens, calls: calls.length, pinned: 0, small: 0, superseded: 0,
    asked: 0, kept: 0, trimmed: 0, stubbed: 0, folded: 0, budgetSteps: 0, structural: 0, targetRatio: 0, targetMet: true, stateTokens: 0, stateStage: 0, requests: 0, jevInputTokens: 0, jevCostUsd: 0,
  }
}

/**
 * The transcript with stale tool outputs shrunk, everything else verbatim. Never touched: every
 * user and assistant text, the first message, the newest `preserveRecentMessages`, calls in
 * flight, and every error output (what went wrong is what the model must not repeat).
 * Throws on a Jev failure, a malformed answer, or a state that cannot fit.
 */
export async function compact(messages: readonly Message[], asker: JevAsker, partial: Partial<CompactOptions> = {}): Promise<CompactResult> {
  const options = resolveOptions(partial)
  let calls = collectToolCalls(messages, options.preserveRecentMessages)
  const stats = newStats(messages, calls)
  // Compression that carries its own exact reconstruction requires no relevance judgment.
  if (options.dedupe) {
    const edits = new Map<string, { result: string }>()
    for (const call of calls) {
      if (call.pinned || call.isError || call.imageTokens) continue
      const result = repeatText(call.resultText)
      if (result !== call.resultText) edits.set(call.tool_use_id, { result })
    }
    const rebuilt = applyEdits(messages, edits)
    stats.structural = rebuilt.filter((m, i) => m !== messages[i]).length
    messages = rebuilt
    calls = collectToolCalls(messages, options.preserveRecentMessages)
  }
  const sources = duplicateSources(calls)
  const canonical = new Set(sources.values())
  // Existing references survive repeated compaction; their original IDs never depend on t1/t2 labels.
  for (const call of calls) {
    for (const match of call.resultText.matchAll(/duplicate of tool_use_id=("(?:[^"\\]|\\.)*")/g)) {
      canonical.add(JSON.parse(match[1]!))
    }
  }
  const decisions: Decision[] = []
  const candidates: ToolCall[] = []
  for (const call of calls) {
    const chars = pairChars(call)
    if (call.pinned || call.isError || hasCompactionNote(call.resultText)) {
      stats.pinned += 1
      decisions.push({ id: call.id, tool: call.tool, action: 'pinned', chars })
    } else if (canonical.has(call.tool_use_id)) {
      stats.kept += 1
      decisions.push({ id: call.id, tool: call.tool, action: 'keep', chars })
    } else if (sources.has(call.id)) {
      stats.superseded += 1
      decisions.push({ id: call.id, tool: call.tool, action: 'superseded', chars, source: sources.get(call.id) })
    } else if (call.resultChars < options.minPairChars) { // nothing to gain on the output
      stats.small += 1
      decisions.push({ id: call.id, tool: call.tool, action: 'small', chars })
    } else {
      candidates.push(call)
    }
  }
  stats.asked = candidates.length

  if (candidates.length > 0) {
    const fitted = fitState(messages, calls, goalOf(messages, options.goal), options.maxStateTokens)
    stats.stateTokens = fitted.tokens
    stats.stateStage = fitted.stage
    const batches = batchCalls(candidates, fitted.tokens, options.maxRequestTokens)
    const responses = await mapBatches(batches, async (batch) => {
      const questions: JevQuestions = {}
      for (const call of batch) Object.assign(questions, questionFor(call))
      const response = await asker.ask(fitted.state, questions)
      if (!response.answers || Array.isArray(response.answers) || Object.keys(response.answers).some((name) => !Object.hasOwn(questions, name))) {
        throw new Error('Jev returned unexpected answers')
      }
      for (const name of Object.keys(questions)) noulOf(response.answers, name)
      return response
    })
    const answers: Record<string, JevAnswer> = {}
    for (const response of responses) {
      Object.assign(answers, response.answers)
      stats.jevInputTokens += response.usage?.input_tokens ?? 0
      stats.jevCostUsd += response.usage?.cost ?? 0
    }
    stats.requests = batches.length
    for (const call of candidates) {
      const decision = decide(call, noulOf(answers, questionName(call)), options)
      decisions.push(decision)
      if (decision.action === 'keep') stats.kept += 1
      else if (decision.action === 'trim') stats.trimmed += 1
      else stats.stubbed += 1
    }
  }
  decisions.sort((a, b) => Number(a.id.slice(1)) - Number(b.id.slice(1)))
  // A soft target is reported after actual serialization, never enforced by deleting evidence.
  const build = (visible?: ReadonlySet<string>) => editsFor(calls, decisions, options, visible)
  // Twice: the notes name only what the conversation no longer shows anywhere after the compaction
  const first = applyEdits(messages, build())
  // A rejected expanding note leaves its original facts visible. Freeze that retention decision
  // before the second pass, otherwise those facts suppress their own index and then disappear.
  const firstResults = new Map(first.flatMap((m) => m.toolResults ?? []).map((r) => [r.tool_use_id, r.text]))
  for (const decision of decisions) {
    const call = calls.find((c) => c.id === decision.id)!
    if (['stub', 'trim', 'superseded'].includes(decision.action) && firstResults.get(call.tool_use_id) === call.resultText) decision.action = 'keep'
  }
  const shown = factsIn(first.map((m) => [m.text, ...m.toolUses.map((u) => JSON.stringify(u.input)),
    ...(m.toolResults ?? []).map((r) => r.text.split('\n').filter((line) => !line.includes(NOTE_TAG)).join('\n'))].join('\n')).join('\n'))
  const output = applyEdits(messages, build(shown))
  // Count realized edits, including candidates rejected by the net-savings guard.
  const outputs = new Map(output.flatMap((m) => m.toolResults ?? []).map((r) => [r.tool_use_id, r.text]))
  for (const decision of decisions) {
    const call = calls.find((c) => c.id === decision.id)!
    if (['stub', 'trim', 'superseded'].includes(decision.action) && outputs.get(call.tool_use_id) === call.resultText) decision.action = 'keep'
  }
  stats.kept = decisions.filter((d) => d.action === 'keep').length
  stats.trimmed = decisions.filter((d) => d.action === 'trim').length
  stats.stubbed = decisions.filter((d) => d.action === 'stub').length
  stats.superseded = decisions.filter((d) => d.action === 'superseded').length
  stats.messagesAfter = output.length
  stats.charsAfter = transcriptChars(output)
  stats.tokensAfter = Math.ceil(stats.charsAfter / 3.2)
  stats.targetRatio = targetRatio(options.targetRatio, stats.tokensBefore)
  stats.targetMet = stats.targetRatio <= 0 || stats.charsAfter <= stats.charsBefore * stats.targetRatio
  return { messages: output, decisions, stats }
}

/** The fraction of characters removed, 0 to 1. */
export function reductionRatio(result: CompactResult): number {
  const { charsBefore, charsAfter } = result.stats
  return charsBefore === 0 ? 0 : (charsBefore - charsAfter) / charsBefore
}

/** One line for the log and the app: what changed and what it cost. */
export function summarize(result: CompactResult): string {
  const s = result.stats
  return `${Math.round(reductionRatio(result) * 100)}% smaller (~${Math.round(s.tokensBefore / 1000)}k to ~${Math.round(s.tokensAfter / 1000)}k tokens); ` +
    `${s.asked} asked; ${s.kept} kept, ${s.trimmed} trimmed, ${s.stubbed} stubbed; ${s.superseded} exact duplicates, ${s.pinned} pinned, ${s.small} small; ` +
    `${s.structural} repeat-compressed; ${s.targetMet ? '' : 'soft size target unmet; '}${s.requests} Jev request(s), $${s.jevCostUsd.toFixed(5)}`
}
