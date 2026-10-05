// Benchmarks the Jev compaction on saved Claude Code transcripts: how much smaller the context a
// new account has to load gets, what each decision was, and what Jev cost.
//
//   OPENROUTER_API_KEY=... bun mods/jev-compact/bench/bench.ts ~/.claude/projects/<project>/<session>.jsonl [...]
//
// Options:
//   --cuts 4          also compact the transcript as it stood at 1/4, 2/4, 3/4 of the way (more data points)
//   --fake 0.5        no network: every answer is this probability (checks the plumbing, costs nothing)
//   --budget 0.15     stop before a request once this many dollars were spent in this run
//   --decisions       print every asked call's decision and probability (to judge the quality by eye)
//   --quality         hindsight test: compact the session as it stood at 1/4, 2/4 and 3/4 of the way, then
//                     count the project facts (names, paths, values; not plain words or standard-library
//                     names) the assistant used from memory after that point that only a removed output
//                     held: what it would have had to get again, or lacked
//
// Token counts are estimates (3.2 characters per token, the same on both sides); the transcript's
// own last `usage` is printed beside them so the estimate can be checked against what Claude saw.

import { readFileSync } from 'node:fs'
import { basename } from 'node:path'

import { compact, reductionRatio, summarize } from '../src/compact.ts'
import { askerOver } from '../src/openrouter.ts'
import type { JevAsker, Message, ToolResult, ToolUse } from '../src/types.ts'

type Block = { type: string; text?: string; id?: string; name?: string; input?: Record<string, unknown>; tool_use_id?: string; content?: unknown; is_error?: boolean }
type Row = { type?: string; subtype?: string; isSidechain?: boolean; isMeta?: boolean; toolUseResult?: unknown; message?: { id?: string; role?: string; content?: string | Block[]; usage?: Record<string, number> } }

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.map((b: Block) => (b.type === 'text' ? b.text ?? '' : '')).join('')
}

/** The main conversation since the last compaction, as `session.compact` would hand it over. */
export function messagesOf(jsonl: string): { messages: Message[]; contextTokens?: number } {
  let messages: Message[] = []
  let contextTokens: number | undefined
  let lastAssistantId: string | undefined
  for (const line of jsonl.split('\n')) {
    if (!line.trim()) continue
    let row: Row
    try { row = JSON.parse(line) as Row } catch { continue }
    if (row.type === 'system' && row.subtype === 'compact_boundary') { messages = []; lastAssistantId = undefined; continue }
    if ((row.type !== 'user' && row.type !== 'assistant') || row.isSidechain || !row.message) continue
    const content = row.message.content
    if (row.type === 'assistant') {
      const usage = row.message.usage
      if (usage) {
        const total = (usage.input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0)
        if (total > 0) contextTokens = total
      }
      const blocks = Array.isArray(content) ? content : []
      const toolUses: ToolUse[] = blocks.filter((b) => b.type === 'tool_use')
        .map((b) => ({ tool_use_id: b.id ?? '', tool: b.name ?? '?', input: b.input ?? {} }))
      const text = textOf(content)
      // Claude Code stores one row per content block of the same reply: one message again
      const last = messages[messages.length - 1]
      if (last && last.role === 'assistant' && row.message.id && row.message.id === lastAssistantId) {
        last.text += text
        last.toolUses.push(...toolUses)
      } else {
        messages.push({ role: 'assistant', text, toolUses })
      }
      lastAssistantId = row.message.id
      continue
    }
    lastAssistantId = undefined
    const blocks = Array.isArray(content) ? content : []
    const toolResults: ToolResult[] = blocks.filter((b) => b.type === 'tool_result')
      .map((b) => {
        const result: ToolResult = { tool_use_id: b.tool_use_id ?? '', text: textOf(b.content), isError: b.is_error === true }
        // An image is in the stored record, as Claude Code hands it to a compaction (Read's has its size)
        if (Array.isArray(b.content) && b.content.some((x: Block) => x.type === 'image')) {
          const stored = row.toolUseResult as { type?: string } | undefined
          result.result = stored?.type === 'image' ? stored : { content: b.content }
        }
        return result
      })
    const message: Message = { role: 'user', text: textOf(content), toolUses: [] }
    if (toolResults.length > 0) message.toolResults = toolResults
    if (message.text || toolResults.length > 0) messages.push(message)
  }
  return contextTokens === undefined ? { messages } : { messages, contextTokens }
}

const FACT = /[A-Za-z_][A-Za-z0-9_./-]{7,}|\b\d{4,}\b/g
// Project facts only: identifier-like (a _ . / digit or mixed case), not a standard-library call or a
// plain word: those the model knows without the conversation
const STDLIB = /^(time|json|os|sys|re|io|math|random|shutil|subprocess|threading|logging|pathlib|Path|datetime|collections|itertools|functools|typing|unittest|self\.assert|Object|Date|Math|JSON|Promise|Array|String|Number|console|process|fs|path|Buffer|Map|Set|Reflect|Symbol|window|document)\.|\.(json|py|ts|js|md|txt|tsx)$/
const projectFact = (f: string) => (/[_./\d]|[a-z][A-Z]/.test(f) && !STDLIB.test(f)) || /^[A-Z][A-Z0-9_]{5,}$/.test(f)
const factsOf = (text: string) => new Set((text.match(FACT) ?? []).filter(projectFact))
const everything = (messages: readonly Message[]) => messages.map((m) =>
  [m.text, ...m.toolUses.map((u) => JSON.stringify(u.input)), ...(m.toolResults ?? []).map((r) => r.text)].join('\n')).join('\n')

/** The hindsight quality test (see --quality above): one line of totals over three cut points. */
export async function hindsight(messages: readonly Message[], asker: JevAsker): Promise<string> {
  let before = 0, saved = 0, cut = 0, needed = 0, lost = 0
  for (const at of [0.25, 0.5, 0.75]) {
    const k = Math.round(messages.length * at)
    const prefix = messages.slice(0, k)
    const result = await compact(prefix, asker)
    before += result.stats.charsBefore
    saved += result.stats.charsBefore - result.stats.charsAfter
    const kept = factsOf(everything(result.messages))
    // used from memory: written by the assistant after the cut before any newer tool output showed it again
    const used = new Set<string>(), seenAgain = new Set<string>()
    for (const m of messages.slice(k)) {
      if (m.role === 'assistant') {
        for (const f of factsOf([m.text, ...m.toolUses.map((u) => JSON.stringify(u.input))].join('\n'))) if (!seenAgain.has(f)) used.add(f)
      }
      for (const r of m.toolResults ?? []) for (const f of factsOf(r.text)) seenAgain.add(f)
    }
    const now = new Map(result.messages.flatMap((m) => (m.toolResults ?? []).map((r) => [r.tool_use_id, r.text] as const)))
    for (const m of prefix) {
      for (const r of m.toolResults ?? []) {
        if (now.get(r.tool_use_id) === r.text) continue
        cut += 1
        const missing = [...factsOf(r.text)].filter((f) => !kept.has(f) && used.has(f))
        if (missing.length >= 2) { needed += 1; lost += missing.length }
      }
    }
  }
  return `${before ? Math.round((saved / before) * 100) : 0}% smaller | outputs cut ${cut}, later needed ${needed} | facts lost that the assistant used later: ${lost}`
}

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  return i >= 0 ? process.argv[i + 1] : undefined
}

async function main(): Promise<void> {
  const flags = new Set(['--cuts', '--fake', '--budget'])
  const files = process.argv.slice(2).filter((a, i, all) => !a.startsWith('--') && !flags.has(all[i - 1] ?? ''))
  if (files.length === 0) {
    console.error('usage: bun bench.ts <transcript.jsonl> [...] [--cuts N] [--fake P] [--budget USD] [--decisions] [--quality]')
    process.exit(2)
  }
  const cuts = Math.max(1, Number(arg('--cuts') ?? 1))
  const fake = arg('--fake')
  const budget = Number(arg('--budget') ?? 0.15)
  const showDecisions = process.argv.includes('--decisions')
  const key = process.env.OPENROUTER_API_KEY
  if (fake === undefined && !key) {
    console.error('OPENROUTER_API_KEY is not set (or pass --fake 0.5 for a dry run)')
    process.exit(2)
  }
  let spent = 0
  const live = askerOver(async (url, init) => {
    const response = await fetch(url, init)
    return { status: response.status, ok: response.ok, text: await response.text() }
  }, { apiKey: key ?? '' })
  const asker: JevAsker = fake !== undefined
    ? { ask: async (_state, questions) => ({ answers: Object.fromEntries(Object.keys(questions).map((n) => [n, { type: 'noul' as const, noul: Number(fake) }])) }) }
    : { ask: async (state, questions) => {
        if (spent >= budget) throw new Error(`budget of $${budget} reached`)
        const response = await live.ask(state, questions)
        spent += response.usage?.cost ?? 0
        return response
      } }

  if (process.argv.includes('--quality')) {
    for (const file of files) console.log(`${basename(file).slice(0, 8)} ${await hindsight(messagesOf(readFileSync(file, 'utf8')).messages, asker)}`)
    console.log(`Jev spent $${spent.toFixed(5)}`)
    return
  }

  const rows: string[] = []
  let totalBefore = 0
  let totalAfter = 0
  for (const file of files) {
    const { messages, contextTokens } = messagesOf(readFileSync(file, 'utf8'))
    let lastSaved: number | undefined
    for (let cut = 1; cut <= cuts; cut++) {
      const part = messages.slice(0, Math.round((messages.length * cut) / cuts))
      if (part.length < 4) continue
      const started = Date.now()
      const result = await compact(part, asker)
      const ms = Date.now() - started
      totalBefore += result.stats.tokensBefore
      totalAfter += result.stats.tokensAfter
      if (cut === cuts) lastSaved = result.stats.tokensBefore - result.stats.tokensAfter
      const label = `${basename(file).slice(0, 8)} ${cut}/${cuts} (${part.length} msgs)`
      rows.push(`${label.padEnd(28)} ${String(Math.round(reductionRatio(result) * 100)).padStart(3)}%  ${summarize(result)}  ${ms} ms`)
      if (showDecisions) {
        for (const d of result.decisions) {
          if (d.need === undefined && d.action !== 'superseded') continue
          console.log(`  ${d.id.padEnd(5)} ${d.tool.padEnd(12)} ${d.action.padEnd(10)} ${d.need === undefined ? '  -  ' : d.need.toFixed(2)}  ${d.chars} chars`)
        }
      }
    }
    if (contextTokens && lastSaved !== undefined) {
      // Claude's own count also holds the system prompt and tool definitions, which no pruning
      // touches: the share of that a swap saves is the honest number
      rows.push(`${basename(file).slice(0, 8)} last reply read ${contextTokens} tokens (Claude's own count): ` +
        `~${Math.round(lastSaved / 1000)}k saved = ~${Math.round((lastSaved / contextTokens) * 100)}% of the new account's cold start`)
    }
  }
  console.log(rows.join('\n'))
  if (totalBefore > 0) {
    console.log(`\nall: ~${Math.round(totalBefore / 1000)}k -> ~${Math.round(totalAfter / 1000)}k tokens, ` +
      `${Math.round((1 - totalAfter / totalBefore) * 100)}% less for the new account to load; Jev spent $${spent.toFixed(5)}`)
  }
}

if (import.meta.main) await main()
