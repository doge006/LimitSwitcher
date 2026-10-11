// Jev's pruning against a /compact-style summary, on the same sessions and cut points.
//
//   OPENROUTER_API_KEY=... bun mods/jev-compact/bench/summary.ts [--summarizers a,b] [--jev] [--cache file] [--budget 0.4]
//
// Claude Code's /compact replaces the whole conversation with a summary the model writes, then
// attaches the files read most recently again. Here a model on OpenRouter writes that summary from
// the same transcript (Claude Code uses the session's own model; a cheaper one stands in), and both
// approaches are scored the same way:
//   after   estimated tokens the next turn starts from (3.2 characters per token)
//   lost    project facts (names, paths, values) the assistant used from memory after the cut that the
//           conversation held before it and the compacted context no longer does (lower is better)

import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { compact } from '../src/compact.ts'
import { factsIn } from '../src/facts.ts'
import { askerOver } from '../src/openrouter.ts'
import type { JevAsker, Message } from '../src/types.ts'
import { synthSession } from './synth.ts'

const POINTS: [seed: number, at: number][] = [[3, 0.5], [3, 0.75], [6, 0.75]]
  .filter((_, i) => !process.env.POINTS || process.env.POINTS.split(',').includes(String(i))) as [number, number][]
const NO_REASONING = /deepseek|haiku/ // off where it can be: the summary is the same job without it
const RESTORE_FILES = 5
const RESTORE_FILE_CHARS = 5000 * 3.2
const RESTORE_TOTAL_CHARS = 50000 * 3.2

const PROMPT = `Your task is to create a detailed summary of the conversation so far. It replaces the whole conversation: the assistant will continue the work from your summary alone, so capture everything needed to carry on without loss.

Include, in sections:
1. Primary request and intent: everything the user asked for, in detail.
2. Key technical concepts.
3. Files and code sections: every file read, edited or created, why it matters, and the important code (signatures, identifiers, values) verbatim.
4. Errors and fixes: each error met and how it was fixed, with user feedback.
5. Problem solving done and ongoing.
6. All user messages that are not tool results.
7. Pending tasks.
8. Current work: precisely what was being worked on right before this summary, with file names and code.
9. Optional next step, in line with the most recent request.

Be thorough with names, paths and values: they are what is lost most easily.`

const textOf = (messages: readonly Message[]) => messages.map((m) =>
  [m.text, ...m.toolUses.map((u) => JSON.stringify(u.input)), ...(m.toolResults ?? []).map((r) => r.text)].join('\n')).join('\n')

/** Facts the assistant wrote after `k` before any newer tool output showed them again. */
function usedLater(messages: readonly Message[], k: number): Set<string> {
  const used = new Set<string>(), seenAgain = new Set<string>()
  for (const m of messages.slice(k)) {
    if (m.role === 'assistant') {
      for (const f of factsIn([m.text, ...m.toolUses.map((u) => JSON.stringify(u.input))].join('\n'))) if (!seenAgain.has(f)) used.add(f)
    }
    for (const r of m.toolResults ?? []) for (const f of factsIn(r.text)) seenAgain.add(f)
  }
  return used
}

function lost(messages: readonly Message[], k: number, compacted: string): number {
  const before = factsIn(textOf(messages.slice(0, k)))
  const after = factsIn(compacted)
  return [...usedLater(messages, k)].filter((f) => before.has(f) && !after.has(f)).length
}

/** The transcript as plain text for the summarizer. */
function render(messages: readonly Message[]): string {
  const ids = new Map<string, string>()
  const lines: string[] = []
  for (const m of messages) {
    if (m.text.trim()) lines.push(`${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text}`)
    for (const u of m.toolUses) { ids.set(u.tool_use_id, u.tool); lines.push(`[Tool call ${u.tool}: ${JSON.stringify(u.input)}]`) }
    for (const r of m.toolResults ?? []) lines.push(`[${ids.get(r.tool_use_id) ?? 'Tool'} result${r.isError ? ' (error)' : ''}]\n${r.text}`)
  }
  return lines.join('\n\n')
}

/** What Claude Code attaches again after a compaction: the files read most recently, each capped. */
function restoredFiles(messages: readonly Message[]): string {
  const reads = new Map<string, string>() // tool_use_id -> path
  const latest = new Map<string, string>() // path -> output, newest last
  for (const m of messages) {
    for (const u of m.toolUses) if (u.tool === 'Read' && typeof u.input.file_path === 'string') reads.set(u.tool_use_id, u.input.file_path)
    for (const r of m.toolResults ?? []) {
      const path = reads.get(r.tool_use_id)
      if (path) { latest.delete(path); latest.set(path, r.text) }
    }
  }
  let total = 0
  const out: string[] = []
  for (const text of [...latest.values()].reverse().slice(0, RESTORE_FILES)) {
    const piece = text.slice(0, RESTORE_FILE_CHARS)
    if (total + piece.length > RESTORE_TOTAL_CHARS) break
    total += piece.length
    out.push(piece)
  }
  return out.join('\n')
}

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  return i >= 0 ? process.argv[i + 1] : undefined
}

const key = process.env.OPENROUTER_API_KEY ?? ''
const cacheFile = arg('--cache')
const cache: Record<string, any> = cacheFile && existsSync(cacheFile) ? JSON.parse(readFileSync(cacheFile, 'utf8')) : {}
const save = () => { if (cacheFile) writeFileSync(cacheFile, JSON.stringify(cache)) }
const budget = Number(arg('--budget') ?? 0.4)
let spent = 0

async function cached<T extends { cost: number }>(id: string, live: () => Promise<T>): Promise<T> {
  const hash = createHash('sha256').update(id).digest('hex')
  if (cache[hash]) return { ...cache[hash], cost: 0 }
  if (spent >= budget) throw new Error(`budget of $${budget} reached`)
  const value = await live()
  spent += value.cost
  cache[hash] = value
  save()
  return value
}

async function summarize(model: string, transcript: string): Promise<{ text: string; cost: number; ms: number }> {
  return cached(JSON.stringify([model, PROMPT, transcript]), async () => {
    const started = performance.now()
    const response = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST',
      headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json', 'x-title': 'LimitSwitcher (compaction benchmark)' },
      body: JSON.stringify({
        model, max_tokens: 16000, usage: { include: true }, reasoning: NO_REASONING.test(model) ? { enabled: false } : { effort: 'low' },
        messages: [{ role: 'user', content: `<conversation>\n${transcript}\n</conversation>\n\n${PROMPT}` }],
      }),
    })
    const body = await response.json() as any
    if (!response.ok || !body.choices) throw new Error(`${model}: ${response.status} ${JSON.stringify(body).slice(0, 300)}`)
    return { text: String(body.choices[0].message.content ?? ''), cost: Number(body.usage?.cost ?? 0), ms: performance.now() - started }
  })
}

const transport = async (url: string, init: { method: string; headers: Record<string, string>; body: string }) => {
  const response = await fetch(url, init)
  return { status: response.status, ok: response.ok, text: await response.text() }
}

function jevAsker(): JevAsker & { cost: number } {
  const live = askerOver(transport, { apiKey: key })
  const self = {
    cost: 0,
    async ask(state: any, questions: any) {
      const r = await cached(JSON.stringify(['jev', state, questions]), async () => {
        const response = await live.ask(state, questions)
        return { response, cost: response.usage?.cost ?? 0 }
      })
      self.cost += r.cost
      return r.response
    },
  }
  return self
}

const root = resolve(import.meta.dir, '../../..')
const sessions = new Map<number, Message[]>()
for (const [seed] of POINTS) if (!sessions.has(seed)) sessions.set(seed, synthSession({ seed, tasks: 14 + (seed % 3) * 6, root }))

const summarizers = (arg('--summarizers') ?? '').split(',').map((m) => m.trim()).filter(Boolean)
const methods: string[] = [...(process.argv.includes('--jev') ? ['jev'] : []), ...summarizers]
console.log(`${'point'.padEnd(12)} ${'method'.padEnd(34)} ${'before'.padStart(7)} ${'after'.padStart(7)} ${'lost'.padStart(5)} ${'of'.padStart(5)}  $`)
const totals = new Map<string, { before: number; after: number; lost: number; used: number; cost: number }>()
for (const [seed, at] of POINTS) {
  const messages = sessions.get(seed)!
  const k = Math.round(messages.length * at)
  const prefix = messages.slice(0, k)
  const before = Math.round(textOf(prefix).length / 3.2)
  const prefixFacts = factsIn(textOf(prefix))
  const used = [...usedLater(messages, k)].filter((f) => prefixFacts.has(f)).length
  const rows: [string, string, number][] = []
  for (const method of methods) {
    try {
      if (method === 'jev') {
        const asker = jevAsker()
        const result = await compact(prefix, asker, JSON.parse(process.env.COMPACT_OPTIONS ?? '{}'))
        rows.push(['jev (prune, today)', textOf(result.messages), asker.cost])
      } else {
        const summary = await summarize(method, render(prefix))
        rows.push([`${method} summary`, summary.text, summary.cost])
        rows.push([`${method} summary + files`, `${summary.text}\n${restoredFiles(prefix)}`, 0])
      }
    } catch (error) {
      console.error(`  ${method} synth${seed}@${at}: ${(error as Error).message}`)
    }
  }
  for (const [name, text, cost] of rows) {
    const after = Math.round(text.length / 3.2)
    const l = lost(messages, k, text)
    const t = totals.get(name) ?? { before: 0, after: 0, lost: 0, used: 0, cost: 0 }
    totals.set(name, { before: t.before + before, after: t.after + after, lost: t.lost + l, used: t.used + used, cost: t.cost + cost })
    console.log(`${`synth${seed}@${at}`.padEnd(12)} ${name.padEnd(34)} ${`${Math.round(before / 1000)}k`.padStart(7)} ${`${Math.round(after / 1000)}k`.padStart(7)} ${String(l).padStart(5)} ${String(used).padStart(5)}  ${cost.toFixed(4)}`)
  }
}
console.log('\ntotal')
for (const [name, t] of totals) {
  console.log(`${name.padEnd(34)} ${`${Math.round(t.before / 1000)}k`.padStart(7)} -> ${`${Math.round(t.after / 1000)}k`.padStart(6)} (${((1 - t.after / t.before) * 100).toFixed(0)}% smaller)  lost ${t.lost} of ${t.used} facts used later  $${t.cost.toFixed(4)}`)
}
console.log(`spent this run: $${spent.toFixed(4)}`)
