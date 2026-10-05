// The shapes the library works over. `Message` is a structural subset of Claude Code's
// `SessionMessage`, so a session transcript passes in as is, and the library has no engine imports
// (the benchmark runs it on saved transcripts outside Claude Code).
//
// Adapted from cc-mod-jev (MIT, Jay W): see THIRD-PARTY-NOTICES.txt.

export type Role = 'user' | 'assistant'

export type ToolUse = {
  tool_use_id: string
  tool: string
  input: Record<string, unknown>
  /** The result as the model read it, once the transcript holds it. */
  text?: string
  isError?: boolean
  result?: unknown
}

export type ToolResult = {
  tool_use_id: string
  text: string
  isError: boolean
  result?: unknown
}

export type Message = {
  role: Role
  text: string
  toolUses: ToolUse[]
  toolResults?: ToolResult[]
  /** The engine's token on a message it handed `session.compact`: kept, the message stays the engine's own, whole. */
  handle?: string
}

/** One tool_use paired with its tool_result, as the pruning sees it. */
export type ToolCall = {
  /** A short label (`t1`, `t2`, ...) used in the state and the questions. */
  id: string
  tool_use_id: string
  tool: string
  input: Record<string, unknown>
  /** Index of the assistant message holding the tool_use. */
  useIndex: number
  /** Index of the user message holding the tool_result; null while in flight. */
  resultIndex: number | null
  resultText: string
  /** The output's text length, plus its images counted as the characters their tokens would be (images.ts). */
  resultChars: number
  /** Estimated tokens of the images in the output (a screenshot read from disk): 0 when it has none. */
  imageTokens: number
  isError: boolean
  /** Never touched: in the first message, among the newest ones, or still in flight. */
  pinned: boolean
}

export type NoulQuestion = {
  type: 'noul'
  instructions: string
  criteria?: { true?: string; false?: string }
}

export type JevQuestions = Record<string, NoulQuestion>
export type JevState = Record<string, unknown>
export type JevAnswer = { type: 'noul'; noul: number }
export type JevUsage = { input_tokens?: number; output_tokens?: number; cost?: number }
export type JevResponse = { model?: string; answers: Record<string, JevAnswer>; usage?: JevUsage }

/** One request to Jev over any transport: the engine's `$.http.fetch`, plain `fetch`, or a fake. */
export type JevAsker = {
  ask(state: JevState, questions: JevQuestions): Promise<JevResponse>
}

/**
 * What happens to one call:
 * - `pinned`: first message, newest messages, in flight, or an error: never touched, never asked about
 * - `small`: too small to be worth a question: kept
 * - `superseded`: a later Read of the same whole file holds the same, newer content: output stubbed, no question
 * - `keep`: Jev says the output is still needed: kept whole
 * - `trim`: Jev is unsure: the output keeps its head and tail
 * - `stub`: Jev says it is no longer needed: the output becomes a one-line note
 *
 * A call is never removed: the model keeps seeing what it did, only bulky outputs it no longer
 * needs shrink, each with a note saying so (so it re-runs the tool instead of guessing).
 */
export type DecisionAction = 'pinned' | 'small' | 'superseded' | 'keep' | 'trim' | 'stub'

export type Decision = {
  id: string
  tool: string
  action: DecisionAction
  /** Jev's probability that the full output is still needed; absent when Jev was not asked. */
  need?: number
  /** Characters the pair carries (input plus result). */
  chars: number
}

export type CompactOptions = {
  /** The ongoing task, in the state; the last user prompts when absent. */
  goal?: string
  /** At or above: kept whole. */
  keepThreshold: number
  /** Below: stubbed. Between the two: trimmed to head and tail. */
  stubThreshold: number
  /** The same two for outputs cheap to get again (file reads and searches: see kinds.ts). */
  cheapKeepThreshold: number
  cheapStubThreshold: number
  preserveRecentMessages: number
  /** Outputs shorter than this many characters are kept without asking. */
  minPairChars: number
  trimHeadChars: number
  trimTailChars: number
  /** A file-writing tool's or script's string inputs longer than this are shortened (what they did is on disk). */
  maxWriteInputChars: number
  maxStateTokens: number
  maxRequestTokens: number
  /** Fold the lines an older output shares with a newer one (see dedupe.ts). */
  dedupe: boolean
  /** Budget mode (budget.ts): above 0, step outputs down until the conversation is this share of its size; -1 (the default) only on long sessions; 0 never. */
  targetRatio: number
}

export type CompactStats = {
  messagesBefore: number
  messagesAfter: number
  charsBefore: number
  charsAfter: number
  tokensBefore: number
  tokensAfter: number
  calls: number
  pinned: number
  small: number
  superseded: number
  asked: number
  kept: number
  trimmed: number
  stubbed: number
  /** Outputs whose lines shown again in a newer output were folded into a note. */
  folded: number
  /** Steps budget mode took (an output kept whole trimmed, or a trimmed one turned into a note). */
  budgetSteps: number
  stateTokens: number
  stateStage: number
  requests: number
  jevInputTokens: number
  jevCostUsd: number
}

export type CompactResult = {
  messages: Message[]
  decisions: Decision[]
  stats: CompactStats
}
