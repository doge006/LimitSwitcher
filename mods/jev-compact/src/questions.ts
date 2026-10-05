// What Jev is asked about each call. One `noul` question per call (cc-mod-jev asks two: whether
// the call and whether its output should stay); calls are never removed here, so only the output's
// question matters, which halves the question tokens.

import { maskSecrets } from './secrets.ts'
import { abridge } from './state.ts'
import type { JevAnswer, JevQuestions, ToolCall } from './types.ts'

export function questionName(call: ToolCall): string {
  return `need_${call.id}`
}

/**
 * Phrased as a statement, so a high probability means "keep". The question carries the call's own
 * input and the start and end of its output (masked): the conversation state Jev also reads is
 * squeezed to fit its window in a long session, and without this it would judge each call nearly
 * blind (measured: answers bunched within 0.11-0.27; with it they spread over 0.15-0.77).
 */
export function questionFor(call: ToolCall): JevQuestions {
  const input = maskSecrets(abridge(JSON.stringify(call.input).replace(/\s+/g, ' '), 300))
  const output = maskSecrets(abridge(call.resultText.replace(/\s+/g, ' ').trim(), 500))
  const shown = call.imageTokens > 0
    ? `Its output is an image (~${call.imageTokens} tokens) the assistant looked at${output ? `, with this text: ${output}` : ''}`
    : `Its output (${call.resultChars} chars), abridged: ${output}`
  return {
    [questionName(call)]: {
      type: 'noul',
      instructions:
        `Tool call ${call.id}: ${call.tool} ${input}\n${shown}\n\n` +
        'This output is still useful for the rest of the task: it is the current view of code or data the assistant ' +
        'is still working on, or holds facts it will need again.',
      criteria: {
        true: 'It will be needed again: later steps depend on what it says.',
        false: 'It is done with: acted on already, superseded by a later call, unrelated to the goal, or cheap to get again by re-running the tool.',
      },
    },
  }
}

/** The probability in one answer; throws when it is missing or malformed. */
export function noulOf(answers: Record<string, JevAnswer>, name: string): number {
  const answer = answers[name]
  if (!answer || typeof answer.noul !== 'number' || !Number.isFinite(answer.noul)) {
    throw new Error(`Jev answer missing or malformed for ${name}`)
  }
  return answer.noul
}
