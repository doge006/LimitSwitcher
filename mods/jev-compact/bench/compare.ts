// Compares decision models (and free baselines) as the judge behind the Jev compaction, on the same
// sessions, cut points and pipeline, so only the judge differs.
//
//   bun mods/jev-compact/bench/compare.ts [session.jsonl ...] [--synth 6] [--models a,b] [--budget 0.50]
//
// Judges:
//   always offline: keep-all (1.0), stub-all (0.0), coin (0.5: what the judge adds over nothing),
//                   recency (newer = more needed), lexical (shares the output's names with the goal and
//                   recent turns: a free scorer), oracle (hindsight labels: the ceiling)
//   --models        any decision model on OpenRouter's decisions endpoint, e.g.
//                   typesafe/jev-1.13,perplexity/pplx-decider-v1-27b (needs OPENROUTER_API_KEY)
//   --synth N       N synthetic sessions built from this repository (bench/synth.ts) besides any files
//
// Each session is compacted as it stood at 1/4, 2/4 and 3/4 of the way. Per judge:
//   smaller   share of characters removed
//   lost      project facts (names, paths, values) the assistant used from memory after the cut that
//             only a removed output held (lower is better)
//   miss      outputs cut that were needed later (>= 2 such facts), of all needed ones
//   AUC       how well the judge's probability ranks the asked outputs needed later above the rest
//             (0.5 = chance, 1 = perfect); n/a for constant judges
//   ms        wall time per compaction (requests run in parallel, as in the mod); $ = summed usage.cost

import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, resolve } from 'node:path'

import { compact } from '../src/compact.ts'
import { factsIn } from '../src/facts.ts'
import { askerOver } from '../src/openrouter.ts'
import { collectToolCalls } from '../src/transcript.ts'
import type { JevAsker, JevQuestions, JevState, Message } from '../src/types.ts'
import { messagesOf } from './bench.ts'
import { synthSession } from './synth.ts'

type Session = { name: string; messages: Message[] }
type Labels = Map<string, boolean> // question name -> needed later

const CUTS = [0.25, 0.5, 0.75]
const RECENT_SHARE = 0.25

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

/** Hindsight label per call of the prefix: needed when >= 2 facts only its output held are used later. */
function labels(messages: readonly Message[], k: number): Labels {
  const prefix = messages.slice(0, k)
  const used = usedLater(messages, k)
  const prose = factsIn(prefix.map((m) => [m.text, ...m.toolUses.map((u) => JSON.stringify(u.input))].join('\n')).join('\n'))
  const out: Labels = new Map()
  for (const call of collectToolCalls(prefix, 0)) {
    const needed = [...factsIn(call.resultText)].filter((f) => used.has(f) && !prose.has(f))
    out.set(`need_${call.id}`, needed.length >= 2)
  }
  return out
}

function auc(points: { p: number; y: boolean }[]): number | null {
  const pos = points.filter((x) => x.y), neg = points.filter((x) => !x.y)
  if (!pos.length || !neg.length) return null
  let wins = 0
  for (const a of pos) for (const b of neg) wins += a.p > b.p ? 1 : a.p === b.p ? 0.5 : 0
  return wins / (pos.length * neg.length)
}

type Judge = { name: string; make: (prefix: Message[], labels: Labels) => JevAsker; live?: boolean }

const constant = (p: number) => () => ({ ask: async (_s: JevState, q: JevQuestions) =>
  ({ answers: Object.fromEntries(Object.keys(q).map((n) => [n, { type: 'noul' as const, noul: p }])) }) })

function recentText(state: JevState): string {
  const conversation = (state.conversation as Record<string, unknown>[] | undefined) ?? []
  const recent = conversation.slice(Math.floor(conversation.length * (1 - RECENT_SHARE)))
  return `${String(state.goal ?? '')}\n${JSON.stringify(recent)}`
}

const OFFLINE: Judge[] = [
  { name: 'keep-all', make: constant(1) },
  { name: 'stub-all', make: constant(0) },
  { name: 'coin', make: constant(0.5) },
  {
    name: 'recency',
    make: (prefix) => {
      const total = Math.max(1, collectToolCalls(prefix, 0).length)
      return { ask: async (_s, q) => ({ answers: Object.fromEntries(Object.keys(q).map((n) =>
        [n, { type: 'noul' as const, noul: Number(n.slice(6)) / total }])) }) }
    },
  },
  {
    name: 'lexical',
    make: () => ({
      ask: async (state, q) => {
        const recent = factsIn(recentText(state))
        return { answers: Object.fromEntries(Object.entries(q).map(([n, question]) => {
          const facts = [...factsIn(question.instructions)]
          const share = facts.length ? facts.filter((f) => recent.has(f)).length / facts.length : 0
          return [n, { type: 'noul' as const, noul: Math.min(1, 0.15 + 0.85 * Math.min(1, share * 3)) }]
        })) }
      },
    }),
  },
  {
    name: 'oracle',
    make: (_p, l) => ({ ask: async (_s, q) => ({ answers: Object.fromEntries(Object.keys(q).map((n) =>
      [n, { type: 'noul' as const, noul: l.get(n) ? 0.9 : 0.1 }])) }) }),
  },
]

type Row = { before: number; after: number; lost: number; needed: number; missed: number; ms: number; cost: number; requests: number; points: { p: number; y: boolean }[]; errors: number }

async function run(judge: Judge, sessions: readonly Session[], spend: { spent: number; budget: number }): Promise<Row> {
  const row: Row = { before: 0, after: 0, lost: 0, needed: 0, missed: 0, ms: 0, cost: 0, requests: 0, points: [], errors: 0 }
  for (const session of sessions) {
    for (const at of CUTS) {
      const k = Math.round(session.messages.length * at)
      const prefix = session.messages.slice(0, k)
      const l = labels(session.messages, k)
      const inner = judge.make(prefix, l)
      const asker: JevAsker = judge.live ? {
        ask: async (s, q) => {
          if (spend.spent >= spend.budget) throw new Error(`budget of $${spend.budget} reached`)
          const r = await inner.ask(s, q)
          spend.spent += r.usage?.cost ?? 0
          return r
        },
      } : inner
      const started = performance.now()
      let result
      try {
        result = await compact(prefix, asker, JSON.parse(process.env.COMPACT_OPTIONS ?? '{}'))
      } catch (error) {
        row.errors += 1
        console.error(`  ${judge.name} ${session.name}@${at}: ${(error as Error).message}`)
        continue
      }
      row.ms += performance.now() - started
      row.before += result.stats.charsBefore
      row.after += result.stats.charsAfter
      row.cost += result.stats.jevCostUsd
      row.requests += result.stats.requests
      for (const d of result.decisions) if (d.need !== undefined) row.points.push({ p: d.need, y: l.get(`need_${d.id}`) === true })
      const kept = factsIn(textOf(result.messages))
      const used = usedLater(session.messages, k)
      const now = new Map(result.messages.flatMap((m) => (m.toolResults ?? []).map((r) => [r.tool_use_id, r.text] as const)))
      for (const call of collectToolCalls(prefix, 0)) {
        const needed = l.get(`need_${call.id}`) === true
        if (needed) row.needed += 1
        if (now.get(call.tool_use_id) === call.resultText) continue
        const missing = [...factsIn(call.resultText)].filter((f) => !kept.has(f) && used.has(f))
        row.lost += missing.length
        if (needed && missing.length >= 2) {
          row.missed += 1
          if (process.env.DEBUG_MISSED) { const d = result.decisions.find((x) => x.id === collectToolCalls(prefix, 8).find((c) => c.tool_use_id === call.tool_use_id)?.id); console.error(`MISSED ${judge.name} ${session.name}@${at} ${call.tool} ${call.resultText.length}ch ${JSON.stringify(call.input).slice(0, 80)} action=${d?.action} need=${d?.need?.toFixed(2)} missing=${missing.slice(0, 6).join(',')}`) }
        }
      }
    }
  }
  return row
}

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  return i >= 0 ? process.argv[i + 1] : undefined
}

async function main(): Promise<void> {
  const valued = new Set(['--synth', '--models', '--budget', '--tasks', '--cache'])
  const files = process.argv.slice(2).filter((a, i, all) => !a.startsWith('--') && !valued.has(all[i - 1] ?? ''))
  const sessions: Session[] = files.map((f) => ({ name: basename(f).slice(0, 8), messages: messagesOf(readFileSync(f, 'utf8')).messages }))
  const synth = Number(arg('--synth') ?? (files.length ? 0 : 6))
  const tasks = Number(arg('--tasks') ?? 14)
  const root = process.env.SYNTH_ROOT ?? resolve(import.meta.dir, '../../..')
  for (let seed = Number(process.env.SYNTH_FROM ?? 1); seed <= synth; seed++) sessions.push({ name: `synth${seed}`, messages: synthSession({ seed, tasks: tasks + (seed % 3) * 6, root }) })
  if (!sessions.length) throw new Error('no sessions')

  const judges = [...OFFLINE]
  const models = (arg('--models') ?? '').split(',').map((m) => m.trim()).filter(Boolean)
  if (models.length) {
    const key = process.env.OPENROUTER_API_KEY
    if (!key) throw new Error('--models needs OPENROUTER_API_KEY')
    const transport = async (url: string, init: { method: string; headers: Record<string, string>; body: string }) => {
      const response = await fetch(url, init)
      return { status: response.status, ok: response.ok, text: await response.text() }
    }
    // --cache file: answers recorded once (keyed by model, state and questions) replay for free
    const cacheFile = arg('--cache')
    const cache: Record<string, unknown> = cacheFile && existsSync(cacheFile) ? JSON.parse(readFileSync(cacheFile, 'utf8')) : {}
    const save = () => { if (cacheFile) writeFileSync(cacheFile, JSON.stringify(cache)) }
    for (const model of models) {
      judges.push({
        name: model, live: true,
        make: () => {
          const live = askerOver(transport, { apiKey: key, model })
          return {
            ask: async (state, questions) => {
              const id = createHash('sha256').update(JSON.stringify([model, state, questions])).digest('hex')
              if (cache[id]) return { ...(cache[id] as Awaited<ReturnType<JevAsker['ask']>>), usage: undefined }
              const response = await live.ask(state, questions)
              cache[id] = response
              save()
              return response
            },
          }
        },
      })
    }
  }

  for (const s of sessions) {
    const chars = s.messages.reduce((n, m) => n + textOf([m]).length, 0)
    console.log(`${s.name.padEnd(9)} ${String(s.messages.length).padStart(4)} msgs  ~${Math.round(chars / 3.2 / 1000)}k tokens`)
  }
  console.log(`\n${'judge'.padEnd(32)} smaller   lost   miss     AUC     ms/compaction  requests  $`)
  const spend = { spent: 0, budget: Number(arg('--budget') ?? 0.5) }
  const runs = sessions.length * CUTS.length
  for (const judge of judges) {
    const r = await run(judge, sessions, spend)
    const a = auc(r.points)
    const done = runs - r.errors
    console.log(`${judge.name.padEnd(32)} ${`${Math.round((1 - r.after / Math.max(1, r.before)) * 100)}%`.padStart(6)} ${String(r.lost).padStart(6)} ` +
      `${`${r.missed}/${r.needed}`.padStart(7)} ${(a === null || new Set(r.points.map((x) => x.p)).size < 2 ? 'n/a' : a.toFixed(3)).padStart(7)} ` +
      `${String(Math.round(r.ms / Math.max(1, done))).padStart(14)} ${String(r.requests).padStart(9)}  ${r.cost.toFixed(5)}${r.errors ? `  (${r.errors} failed)` : ''}`)
  }
}

if (import.meta.main) await main()
