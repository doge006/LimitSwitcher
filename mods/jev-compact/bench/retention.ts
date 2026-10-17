// Offline only: synthetic evidence with exact continuation checks; no keys or network.
// bun mods/jev-compact/bench/retention.ts [--baseline /absolute/path/to/old/src/compact.ts]
// A caller may export the tracked baseline modules into a temporary directory. Never use transcripts.
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { compact } from '../src/compact.ts'
import { expandRepeats } from '../src/dedupe.ts'
import { estimateTokens } from '../src/tokens.ts'
import type { JevAsker, Message } from '../src/types.ts'

function fixture(seed: number, repeats: boolean): Message[] {
  const messages: Message[] = [{role: 'user', text: 'Compare before and after. Preserve units, input recipes, errors and the unfinished download check.', toolUses: []}]
  const pair = (id: string, tool: string, input: Record<string, unknown>, text: string, isError = false) => {
    messages.push({role: 'assistant', text: '', toolUses: [{tool_use_id: id, tool, input}]},
      {role: 'user', text: '', toolUses: [], toolResults: [{tool_use_id: id, text, isError}]})
  }
  const view = (value: string) => Array.from({length: 180}, (_, i) => i === 90 ? `timeout = ${value} ms; applies only to uploads` : `row_${seed}_${i}: value_${i}`).join('\n')
  pair('before', 'Read', {file_path: '/synthetic/config'}, view('2.5'))
  pair('after', 'Read', {file_path: '/synthetic/config'}, view('7.5'))
  const recipe = 'setup\n'.repeat(200) + 'x = 2.5; unit = "ms"\n' + 'finish\n'.repeat(200)
  pair('recipe', 'Bash', {command: recipe}, 'completed')
  pair('error', 'Test', {case: 'downloads'}, 'download result was 2.5 ms; expected 7.5 ms\n'.repeat(80), true)
  for (let i = 0; i < 70; i++) {
    const line = `    sample_${seed}_${i} = ${seed / 10} ms; uploads only`
    const text = repeats ? (line + '\n').repeat(600) : Array.from({length: 220}, (_, n) => `${line}; observation ${n}`).join('\n')
    pair(`sample-${i}`, 'Observe', {experiment: `${seed}-${i}`}, text)
  }
  pair('copy-a', 'Read', {file_path: '/synthetic/shared'}, view('1.5'))
  pair('copy-b', 'Read', {file_path: '/synthetic/shared'}, view('1.5'))
  messages.push({role: 'assistant', text: 'Uploads changed. Download behavior still needs verification.', toolUses: []})
  return messages
}

function evidence(messages: Message[], id: string): string {
  const outputs = new Map(messages.flatMap(m => m.toolResults ?? []).map(r => [r.tool_use_id, r.text]))
  const text = outputs.get(id) ?? ''
  const pointer = /^\[LimitSwitcher duplicate of tool_use_id=("(?:[^"\\]|\\.)*");/.exec(text)
  return expandRepeats(pointer ? outputs.get(JSON.parse(pointer[1]!)) ?? '' : text)
}

function checks(original: Message[], after: Message[]) {
  const results: boolean[] = [JSON.stringify(original.map(m => m.text)) === JSON.stringify(after.map(m => m.text))]
  for (const use of original.flatMap(m => m.toolUses)) {
    const current = after.flatMap(m => m.toolUses).find(u => u.tool_use_id === use.tool_use_id)
    results.push(JSON.stringify(current?.input) === JSON.stringify(use.input))
    results.push(evidence(after, use.tool_use_id) === evidence(original, use.tool_use_id))
  }
  return {passed: results.filter(Boolean).length, total: results.length}
}

const baselineIndex = process.argv.indexOf('--baseline')
const methods: [string, typeof compact][] = [['proposed', compact]]
if (baselineIndex >= 0) {
  const file = process.argv[baselineIndex + 1]
  if (!file) throw new Error('--baseline requires a path to a trusted local compact.ts')
  methods.unshift(['baseline', (await import(pathToFileURL(resolve(file)).href)).compact])
}
for (const seed of [31, 47, 83]) for (const repeats of [true, false]) {
  const original = fixture(seed, repeats)
  for (const [method, run] of methods) {
    let scorerTokens = 0, requests = 0
    const fake: JevAsker = {ask: async (state, questions) => {
      requests++
      scorerTokens += estimateTokens(JSON.stringify({state, questions}))
      return {answers: Object.fromEntries(Object.keys(questions).map(name => [name, {type: 'noul', noul: .99}]))}
    }}
    const first = await run(original, fake, {preserveRecentMessages: 0})
    const second = await run(first.messages, fake, {preserveRecentMessages: 0})
    console.log(JSON.stringify({seed, profile: repeats ? 'repetitive' : 'unique', method,
      before: first.stats.tokensBefore, after: first.stats.tokensAfter, second: second.stats.tokensAfter,
      checks: checks(original, second.messages), requests, scorerTokens, cost: 'unmeasured; offline fake scorer'}))
  }
}
