// Jev's pruning against a /compact-style summary, on the same sessions and cut points.
//
//   OPENROUTER_API_KEY=... bun mods/jev-compact/bench/summary.ts [--oracle] [--jev] [--summarizers a,b] [--cache file] [--budget 0.05]
//   (--budget 0, the default, replays cached answers only; POINTS=0,1 picks cut points; TAIL_SHARES=0.25,0.1; BREAKDOWN=1)
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
import { factIndex, factsIn, indexBudget } from '../src/facts.ts'
import { askerOver } from '../src/openrouter.ts'
import { collectToolCalls } from '../src/transcript.ts'
import type { JevAsker, Message } from '../src/types.ts'
import { synthSession } from './synth.ts'

const POINTS: [seed: number, at: number][] = [[3, 0.5], [3, 0.75], [6, 0.75]]
  .filter((_, i) => !process.env.POINTS || process.env.POINTS.split(',').includes(String(i))) as [number, number][]
const NO_REASONING = /deepseek|haiku/ // off where it can be: the summary is the same job without it
const TAIL_SHARES = (process.env.TAIL_SHARES ?? '0.25,0.1').split(',').map(Number)
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

/**
 * The stricter count: facts used later with no whole line that held them left in the compacted
 * context (a list of names can't keep these; a kept output, head or code snippet can). A fact that
 * never sat on a line of its own (under 16 characters) counts by name.
 */
function linesLost(messages: readonly Message[], k: number, compacted: string): number {
  const after = new Set(compacted.split('\n').map((l) => l.trim()).filter((l) => l.length >= 16))
  const afterFacts = factsIn(compacted)
  const holders = new Map<string, string[]>()
  for (const line of textOf(messages.slice(0, k)).split('\n')) {
    const t = line.trim()
    if (t.length < 16) continue
    for (const f of factsIn(t)) (holders.get(f) ?? holders.set(f, []).get(f)!).push(t)
  }
  const before = factsIn(textOf(messages.slice(0, k)))
  let lost = 0
  for (const f of usedLater(messages, k)) {
    if (!before.has(f)) continue
    const lines = holders.get(f)
    if (lines ? !lines.some((l) => after.has(l)) : !afterFacts.has(f)) lost += 1
  }
  return lost
}

/** Where the newest `share` of the messages starts: at a prompt of the person's, so no tool result loses its call. */
function tailStart(messages: readonly Message[], share: number): number {
  for (let i = Math.floor(messages.length * (1 - share)); i < messages.length; i++) {
    const m = messages[i]!
    if (m.role === 'user' && !(m.toolResults?.length) && m.text.trim()) return i
  }
  return messages.length
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
function restoredFiles(messages: readonly Message[], skip: ReadonlySet<string> = new Set()): string {
  const reads = new Map<string, string>() // tool_use_id -> path
  const latest = new Map<string, string>() // path -> output, newest last
  for (const m of messages) {
    for (const u of m.toolUses) if (u.tool === 'Read' && typeof u.input.file_path === 'string' && !skip.has(u.input.file_path)) reads.set(u.tool_use_id, u.input.file_path)
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
const budget = Number(arg('--budget') ?? 0) // 0: cached answers only, nothing new is bought
let spent = 0

async function cached<T extends { cost: number }>(id: string, live: () => Promise<T>): Promise<T> {
  const hash = createHash('sha256').update(id).digest('hex')
  if (cache[hash]) return { ...cache[hash], cost: 0 }
  if (spent >= budget || budget <= 0) throw new Error(`budget of $${budget} reached`)
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
const methods: string[] = [...(process.argv.includes('--oracle') ? ['oracle'] : []), ...(process.argv.includes('--jev') ? ['jev'] : []), ...summarizers]
console.log(`${'point'.padEnd(12)} ${'method'.padEnd(40)} ${'before'.padStart(7)} ${'after'.padStart(7)} ${'lost'.padStart(5)} ${'strict'.padStart(6)} ${'of'.padStart(5)}  $`)
const totals = new Map<string, { before: number; after: number; lost: number; strict: number; used: number; cost: number }>()
for (const [seed, at] of POINTS) {
  const messages = sessions.get(seed)!
  const k = Math.round(messages.length * at)
  const prefix = messages.slice(0, k)
  const before = Math.round(textOf(prefix).length / 3.2)
  const prefixFacts = factsIn(textOf(prefix))
  const used = [...usedLater(messages, k)].filter((f) => prefixFacts.has(f)).length
  const rows: [string, string, number][] = []
  let pruned: Message[] | undefined
  for (const method of methods) {
    try {
      if (method === 'oracle') {
        // The ceiling for any judge in Jev's place: hindsight labels (an output is needed when it holds
        // >= 2 facts used later that the conversation's own text doesn't), as in compare.ts
        const used = usedLater(messages, k)
        const prose = factsIn(prefix.map((m) => [m.text, ...m.toolUses.map((u) => JSON.stringify(u.input))].join('\n')).join('\n'))
        const need = new Map(collectToolCalls(prefix, 0).map((c) => [`need_${c.id}`, [...factsIn(c.resultText)].filter((f) => used.has(f) && !prose.has(f)).length >= 2] as const))
        const oracle: JevAsker = { ask: async (_s, q) => ({ answers: Object.fromEntries(Object.keys(q).map((n) => [n, { type: 'noul' as const, noul: need.get(n) ? 0.9 : 0.1 }])) }) }
        const result = await compact(prefix, oracle, JSON.parse(process.env.COMPACT_OPTIONS ?? '{}'))
        rows.push(['oracle judge (ceiling)', textOf(result.messages), 0])
      } else if (method === 'jev') {
        const asker = jevAsker()
        const result = await compact(prefix, asker, JSON.parse(process.env.COMPACT_OPTIONS ?? '{}'))
        pruned = result.messages
        if (process.env.BREAKDOWN) {
          const parts: Record<string, number> = {}
          const add = (k: string, n: number) => (parts[k] = (parts[k] ?? 0) + n)
          const action = new Map(result.decisions.map((d) => [d.id, d.action] as const))
          const byUse = new Map(collectToolCalls(prefix, 8).map((c) => [c.tool_use_id, c] as const))
          for (const m of result.messages) {
            add(`${m.role} text`, m.text.length)
            for (const u of m.toolUses) add('tool inputs', JSON.stringify(u.input).length)
            for (const r of m.toolResults ?? []) { const c = byUse.get(r.tool_use_id); add(`output ${c ? (c.pinned ? 'pinned/recent' : c.isError ? 'error' : action.get(c.id) ?? '?') : 'recent'}`, r.text.length) }
          }
          console.log(`  jev breakdown synth${seed}@${at}: ` + Object.entries(parts).sort((x, y) => y[1] - x[1]).map(([k, v]) => `${k} ${Math.round(v / 3200)}k`).join(', '))
        }
        rows.push(['jev (prune, today)', textOf(result.messages), asker.cost])
      } else {
        const summary = await summarize(method, render(prefix))
        const short = method.split('/').pop()!
        rows.push([`${short} summary`, summary.text, summary.cost])
        rows.push([`${short} summary + files`, `${summary.text}\n${restoredFiles(prefix)}`, 0])
        if (!pruned) continue
        // Hybrid: the summary in place of the older part, the newest share as Jev left it, the files read
        // most recently before that, and the names, paths and values the older part held that nothing kept shows
        for (const share of TAIL_SHARES) {
          const split = tailStart(prefix, share)
          const tail = textOf(pruned.slice(split))
          const inTail = new Set(prefix.slice(split).flatMap((m) => m.toolUses.map((u) => String(u.input.file_path ?? ''))))
          const files = restoredFiles(prefix.slice(0, split), inTail)
          const base = `${summary.text}\n${files}\n${tail}`
          rows.push([`hybrid ${short} tail ${share}`, base, 0])
          const older = textOf(prefix.slice(0, split))
          const index = factIndex(older, factsIn(base), indexBudget(older.length), factsIn(base))
          rows.push([`hybrid ${short} tail ${share} + index`, `${base}\n${index.join(', ')}`, 0])
        }
      }
    } catch (error) {
      console.error(`  ${method} synth${seed}@${at}: ${(error as Error).message}`)
    }
  }
  for (const [name, text, cost] of rows) {
    const after = Math.round(text.length / 3.2)
    const l = lost(messages, k, text)
    const strict = linesLost(messages, k, text)
    const t = totals.get(name) ?? { before: 0, after: 0, lost: 0, strict: 0, used: 0, cost: 0 }
    totals.set(name, { before: t.before + before, after: t.after + after, lost: t.lost + l, strict: t.strict + strict, used: t.used + used, cost: t.cost + cost })
    console.log(`${`synth${seed}@${at}`.padEnd(12)} ${name.padEnd(40)} ${`${Math.round(before / 1000)}k`.padStart(7)} ${`${Math.round(after / 1000)}k`.padStart(7)} ${String(l).padStart(5)} ${String(strict).padStart(6)} ${String(used).padStart(5)}  ${cost.toFixed(4)}`)
  }
}
console.log('\ntotal')
for (const [name, t] of totals) {
  console.log(`${name.padEnd(40)} ${`${Math.round(t.before / 1000)}k`.padStart(7)} -> ${`${Math.round(t.after / 1000)}k`.padStart(6)} (${((1 - t.after / t.before) * 100).toFixed(0)}% smaller)  lost ${t.lost}, strict ${t.strict}, of ${t.used} facts used later  $${t.cost.toFixed(4)}`)
}
console.log(`spent this run: $${spent.toFixed(4)}`)
