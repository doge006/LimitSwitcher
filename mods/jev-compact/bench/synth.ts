// Synthetic Claude Code sessions for the model comparison (compare.ts), built from this repository's
// own files so the tool outputs are real code. A session is a run of tasks; each task explores a few
// files it then leaves alone (decoys), works on one or two (targets: read, re-read, edited, tested),
// and the assistant names the targets' identifiers from memory. Some later tasks go back to an
// earlier task's target without reading it again: the dependency a compaction must not cut.
//
// What is "needed" is not set here: compare.ts labels every output by hindsight, the same way for
// real and synthetic sessions.

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { factsIn } from '../src/facts.ts'
import type { Message } from '../src/types.ts'

export type SynthOptions = { seed: number; tasks: number; root: string }

/** Small seeded generator (mulberry32): the same seed builds the same session. */
export function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const SOURCE = /\.(py|ts|md)$/
const SKIP = /node_modules|\.git\/|__pycache__|CHANGELOG|tests?\//

function sourceFiles(root: string): { path: string; text: string }[] {
  const out: { path: string; text: string }[] = []
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name)
      if (SKIP.test(full + '/')) continue
      const st = statSync(full)
      if (st.isDirectory()) walk(full)
      else if (SOURCE.test(name) && st.size > 2500 && st.size < 60000) out.push({ path: relative(root, full), text: readFileSync(full, 'utf8') })
    }
  }
  walk(root)
  return out.sort((a, b) => a.path.localeCompare(b.path))
}

/** Read's output format: numbered lines. */
function numbered(text: string, from = 1, to = Infinity): string {
  return text.split('\n').slice(from - 1, to).map((line, i) => `${String(from + i).padStart(6)}→${line}`).join('\n')
}

function pick<T>(r: () => number, items: readonly T[]): T {
  return items[Math.floor(r() * items.length)]!
}

function sample<T>(r: () => number, items: readonly T[], n: number): T[] {
  const copy = [...items]
  const out: T[] = []
  while (out.length < n && copy.length) out.push(copy.splice(Math.floor(r() * copy.length), 1)[0]!)
  return out
}

function testLog(r: () => number, names: readonly string[], failing: string | null): string {
  const lines = ['============================= test session starts ==============================', 'platform linux -- Python 3.12.3, pytest-8.3.2']
  for (let i = 0; i < 60 + Math.floor(r() * 120); i++) {
    const name = names.length ? pick(r, names) : `case_${i}`
    lines.push(`tests/test_${name.toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 30)}.py::test_${i} PASSED`)
  }
  if (failing) lines.push(`FAILED tests/test_core.py::test_${failing} - AssertionError: expected ${failing} to be set`)
  lines.push(failing ? '=========== 1 failed, 140 passed in 3.21s ===========' : '=========== 141 passed in 3.02s ===========')
  return lines.join('\n')
}

/** One synthetic session, oldest message first. */
export function synthSession(options: SynthOptions): Message[] {
  const r = rng(options.seed)
  const files = sourceFiles(options.root)
  if (files.length < 8) throw new Error(`too few source files under ${options.root}`)
  const messages: Message[] = []
  let n = 0
  const call = (tool: string, input: Record<string, unknown>, output: string, isError = false, said = '') => {
    const id = `toolu_s${options.seed}_${++n}`
    messages.push({ role: 'assistant', text: said, toolUses: [{ tool_use_id: id, tool, input }] })
    messages.push({ role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: id, text: output, isError }] })
  }
  const say = (text: string) => messages.push({ role: 'assistant', text, toolUses: [] })
  const factsOf = (text: string) => [...factsIn(text)].filter((f) => f.length < 40)
  const done: { path: string; facts: string[] }[] = []

  for (let task = 0; task < options.tasks; task++) {
    const [target, second, ...decoys] = sample(r, files, 2 + 3 + Math.floor(r() * 4))
    const targets = r() < 0.5 ? [target!] : [target!, second!]
    const tFacts = factsOf(targets.map((t) => t.text).join('\n'))
    const focus = sample(r, tFacts, 6)
    messages.push({ role: 'user', text: `Task ${task + 1}: in ${target!.path}, change how ${focus[0] ?? 'it'} is handled and keep the tests green.`, toolUses: [] })

    // Explore: a search across the tree, the decoys and the targets, a listing, the history
    const term = focus[0] ?? 'def '
    const hits = files.flatMap((f) => f.text.split('\n').map((line, i) => [f.path, i + 1, line] as const))
      .filter(([, , line]) => line.includes(term.split('.')[0]!)).slice(0, 80)
      .map(([p, i, line]) => `${p}:${i}:${line}`).join('\n')
    call('Grep', { pattern: term, output_mode: 'content' }, hits || 'No matches found', false, 'Looking for where it is used.')
    for (const d of [...decoys, ...targets]) call('Read', { file_path: `/repo/${d.path}` }, numbered(d.text))
    call('Bash', { command: 'git log --oneline -15' }, Array.from({ length: 15 }, (_, i) => `${(Math.floor(r() * 0xfffffff)).toString(16).padStart(7, '0')} ${pick(r, ['fix', 'feat', 'chore'])}: ${pick(r, tFacts) ?? 'update'} (${i})`).join('\n'))
    say(`The work is in ${targets.map((t) => t.path).join(' and ')}; ${decoys.map((d) => d.path).join(', ')} are not involved.`)

    // Work: re-read a part, edit, test (sometimes failing first), and say what changed by name
    const t = targets[0]!
    const lines = t.text.split('\n')
    const from = 1 + Math.floor(r() * Math.max(1, lines.length - 80))
    call('Bash', { command: `sed -n ${from},${from + 80}p ${t.path}` }, lines.slice(from - 1, from + 80).join('\n'))
    const old = lines.slice(from + 5, from + 9).join('\n')
    call('Edit', { file_path: `/repo/${t.path}`, old_string: old, new_string: `${old}\n# handled: ${focus[0] ?? ''}` }, `The file /repo/${t.path} has been updated.`)
    const failing = r() < 0.4 ? focus[1] ?? null : null
    call('Bash', { command: 'python -m pytest -q' }, testLog(r, tFacts, failing), failing !== null)
    if (failing) {
      say(`${failing} is not set on that path; fixing it next to ${focus[2] ?? focus[0]}.`)
      call('Edit', { file_path: `/repo/${t.path}`, old_string: focus[2] ?? 'x', new_string: `${focus[2] ?? 'x'}  # ${failing}` }, `The file /repo/${t.path} has been updated.`)
      call('Bash', { command: 'python -m pytest -q' }, testLog(r, tFacts, null))
    }
    say(`Done: ${focus.slice(0, 4).map((f) => `\`${f}\``).join(', ')} in ${t.path} now handle it.`)

    // A later task goes back to an earlier target from memory, without reading it again
    if (done.length && r() < 0.45) {
      const back = pick(r, done)
      const used = sample(r, back.facts, 3)
      messages.push({ role: 'user', text: `Does ${back.path} still match that?`, toolUses: [] })
      say(`Yes: ${used.map((f) => `\`${f}\``).join(', ')} in ${back.path} line up with the change.`)
    }
    done.push({ path: t.path, facts: tFacts })
  }
  messages.push({ role: 'user', text: 'Thanks, wrap up.', toolUses: [] })
  say('All tasks done; tests pass.')
  return messages
}
