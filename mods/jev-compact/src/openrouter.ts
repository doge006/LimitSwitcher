// Jev through OpenRouter's decisions endpoint, over any transport (the engine's `$.http.fetch`,
// plain `fetch` in the benchmark, or a fake in the tests).
//
// Adapted from cc-mod-jev (MIT, Jay W): see THIRD-PARTY-NOTICES.txt.

import type { JevAsker, JevQuestions, JevResponse, JevState } from './types.ts'

export const OPENROUTER_DECISIONS_URL = 'https://openrouter.ai/api/alpha/decisions'
export const DEFAULT_MODEL = 'typesafe/jev-1.13'

export type TransportResponse = { status: number; ok: boolean; text: string }

export type Transport = (
  url: string,
  init: { method: string; headers: Record<string, string>; body: string },
) => Promise<TransportResponse>

export type ClientConfig = { apiKey: string; model?: string; baseUrl?: string }

/** Deadline covers all batches. Late network replies cannot commit edits or launch more requests. */
export function withDeadline<T>(run: (active: () => boolean) => Promise<T>, schedule: (expire: () => void) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let active = true
    schedule(() => {
      if (!active) return
      active = false
      reject(new Error('Jev compaction deadline exceeded'))
    })
    Promise.resolve().then(() => run(() => active)).then(
      (value) => { if (active) { active = false; resolve(value) } },
      (error) => { if (active) { active = false; reject(error) } },
    )
  })
}

/** Validates a response body; throws on anything but an `answers` object. Never echoes the request (it holds the key). */
export function parseResponse(status: number, ok: boolean, text: string): JevResponse {
  if (!ok) throw new Error(`Jev request failed (${status}): ${text.slice(0, 200)}`)
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('Jev returned malformed JSON')
  }
  if (parsed === null || typeof parsed !== 'object' || !('answers' in parsed) ||
      parsed.answers === null || typeof parsed.answers !== 'object' || Array.isArray(parsed.answers)) {
    throw new Error('Jev response is missing answers')
  }
  return parsed as JevResponse
}

export function askerOver(transport: Transport, config: ClientConfig): JevAsker {
  return {
    async ask(state: JevState, questions: JevQuestions) {
      const response = await transport(config.baseUrl ?? OPENROUTER_DECISIONS_URL, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${config.apiKey}`,
          'content-type': 'application/json',
          'x-title': 'LimitSwitcher (Jev compaction)',
        },
        body: JSON.stringify({ model: config.model ?? DEFAULT_MODEL, state, questions }),
      })
      return parseResponse(response.status, response.ok, response.text)
    },
  }
}
