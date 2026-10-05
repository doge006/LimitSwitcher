import { describe, expect, test } from 'claude-code/testing'

import { NOTE_TAG, shortenInput, trimText } from '../src/apply.ts'
import { batchCalls, compact, decide, reductionRatio, resolveOptions } from '../src/compact.ts'
import { imageTokensOf } from '../src/images.ts'
import { cheapToRedo } from '../src/kinds.ts'
import { LONG_SESSION_TOKENS } from '../src/budget.ts'
import { fold, lineKey } from '../src/dedupe.ts'
import { parseResponse } from '../src/openrouter.ts'
import { maskSecrets } from '../src/secrets.ts'
import { goalOf, withoutReminders } from '../src/state.ts'
import { collectToolCalls, supersededCalls } from '../src/transcript.ts'
import type { JevAsker, JevQuestions, JevState, Message } from '../src/types.ts'
import { keyInEnvFile } from '../hooks/register.ts'

const user = (text: string, handle?: string): Message => ({ role: 'user', text, toolUses: [], ...(handle ? { handle } : {}) })
const said = (text: string): Message => ({ role: 'assistant', text, toolUses: [] })
const use = (id: string, tool: string, input: Record<string, unknown>): Message =>
  ({ role: 'assistant', text: '', toolUses: [{ tool_use_id: id, tool, input }], handle: `use-${id}` })
const out = (id: string, text: string, isError = false): Message =>
  ({ role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: id, text, isError }], handle: `res-${id}` })

const FILE = 'export const a = 1\n'.repeat(200)          // 3800 chars
const LOG = 'step ok\n'.repeat(800)                      // 6400 chars
const FAIL = 'FAIL a.test.ts: expected 3 got 2\n'.repeat(40)

/** A session: read a file, run a noisy build, hit an error, read the file again, edit it, then the recent tail. */
function session(): Message[] {
  return [
    user('<system-reminder>noise</system-reminder>Fix the failing test in a.ts', 'h0'),
    use('r1', 'Read', { file_path: '/p/a.ts' }), out('r1', FILE),
    use('b1', 'Bash', { command: 'npm run build' }), out('b1', LOG),
    use('t1', 'Bash', { command: 'npm test' }), out('t1', FAIL, true),
    use('r2', 'Read', { file_path: '/p/a.ts' }), out('r2', FILE),
    use('w1', 'Write', { file_path: '/p/b.ts', content: 'x'.repeat(5000) }), out('w1', 'File created'),
    use('g1', 'Grep', { pattern: 'a' }), out('g1', 'a.ts:1\n'.repeat(100)),
    said('Now fixing.'),
    // the newest messages (pinned by preserveRecentMessages)
    user('go on'), use('e1', 'Edit', { file_path: '/p/a.ts', old_string: '1', new_string: '2' }), out('e1', 'ok'),
    use('t2', 'Bash', { command: 'npm test' }), out('t2', 'PASS\n'.repeat(200)), said('Done.'),
  ]
}

type Asked = { state: JevState; questions: JevQuestions }

function fakeJev(need: (name: string) => number): JevAsker & { asked: Asked[] } {
  const asked: Asked[] = []
  return {
    asked,
    async ask(state, questions) {
      asked.push({ state, questions })
      const answers = Object.fromEntries(Object.keys(questions).map((name) => [name, { type: 'noul' as const, noul: need(name) }]))
      return { answers, usage: { input_tokens: 900, cost: 0.00004 } }
    },
  }
}

const byLabel = (messages: Message[], id: string) =>
  messages.flatMap((m) => m.toolResults ?? []).find((r) => r.tool_use_id === id)!

describe('compact', () => {
  test('never touches text, errors, the first or the newest messages; stubs only what Jev calls stale', async () => {
    const before = session()
    const jev = fakeJev((name) => (name === 'need_t2' ? 0.05 : 0.9)) // t2 is the build log
    const result = await compact(before, jev, { preserveRecentMessages: 7 })

    expect(result.messages).toHaveLength(before.length)
    result.messages.forEach((m, i) => {
      expect(m.text).toBe(before[i]!.text)
      expect(m.toolUses.map((u) => u.tool_use_id)).toEqual(before[i]!.toolUses.map((u) => u.tool_use_id))
      expect((m.toolResults ?? []).map((r) => r.tool_use_id)).toEqual((before[i]!.toolResults ?? []).map((r) => r.tool_use_id))
    })
    expect(byLabel(result.messages, 'b1').text).toContain(`${NOTE_TAG} removed this Bash output`)
    expect(byLabel(result.messages, 't1').text).toBe(FAIL)                 // an error: pinned
    expect(byLabel(result.messages, 'r1').text).toContain('read again later')  // superseded by r2
    expect(byLabel(result.messages, 'r2').text).toBe(FILE)                 // the latest read stays
    expect(byLabel(result.messages, 't2').text).toBe('PASS\n'.repeat(200)) // newest: pinned
    // untouched messages are the same objects, handles and all
    expect(result.messages[0]).toBe(before[0]!)
    expect(result.messages[13]).toBe(before[13]!)
    expect(result.messages[4]!.handle).toBeUndefined()                    // rebuilt
    expect(result.stats.superseded).toBe(1)
    expect(result.stats.stubbed).toBe(1)
    expect(reductionRatio(result)).toBeGreaterThan(0.3)
  })

  test('the error, the superseded read and the small result are not asked about', async () => {
    const jev = fakeJev(() => 0.9)
    await compact(session(), jev, { preserveRecentMessages: 7 })
    const names = Object.keys(jev.asked[0]!.questions).sort()
    // t1 b1, t5 w1 (Write), t6 g1; not t2 (error), t3/t4 r1 (superseded) / r2 ...
    const calls = collectToolCalls(session(), 7)
    const label = (id: string) => calls.find((c) => c.tool_use_id === id)!.id
    expect(names).toContain(`need_${label('b1')}`)
    expect(names).toContain(`need_${label('r2')}`)
    expect(names).not.toContain(`need_${label('t1')}`)
    expect(names).not.toContain(`need_${label('r1')}`)
    expect(names).not.toContain(`need_${label('e1')}`)
  })

  test('an unsure answer keeps the head and tail', async () => {
    const result = await compact(session(), fakeJev(() => 0.4), { preserveRecentMessages: 7 })
    const text = byLabel(result.messages, 'b1').text
    expect(text.startsWith('step ok')).toBe(true)
    expect(text.endsWith('step ok\n')).toBe(true)
    expect(text).toContain('from the middle of this output')
    expect(text.length).toBeLessThan(LOG.length)
  })

  test("a Write's long content is shortened (the file is on disk), its call stays", async () => {
    const result = await compact(session(), fakeJev(() => 0.9), { preserveRecentMessages: 7 })
    const write = result.messages.flatMap((m) => m.toolUses).find((u) => u.tool_use_id === 'w1')!
    expect(write.input.file_path).toBe('/p/b.ts')
    expect(String(write.input.content).length).toBeLessThan(800)
    expect(String(write.input.content)).toContain('the file on disk has them')
  })

  test("an old long script's input is shortened; a recent one stays whole", async () => {
    const script = 'python3 - <<EOF\n' + 'edit()\n'.repeat(1000) + 'EOF'
    const long = session()
    long[3] = use('b1', 'Bash', { command: script, description: 'apply the edits' })
    long[17] = use('t2', 'Bash', { command: script })
    const result = await compact(long, fakeJev(() => 0.9), { preserveRecentMessages: 7 })
    const old = result.messages.flatMap((m) => m.toolUses).find((u) => u.tool_use_id === 'b1')!
    expect(String(old.input.command).length).toBeLessThan(2200)
    expect(String(old.input.command)).toContain('what the script did is on disk')
    expect(old.input.description).toBe('apply the edits')
    const recent = result.messages.flatMap((m) => m.toolUses).find((u) => u.tool_use_id === 't2')!
    expect(recent.input.command).toBe(script)
  })

  test('the state Jev reads has no system reminders and no full outputs', async () => {
    const jev = fakeJev(() => 0.9)
    await compact(session(), jev, { preserveRecentMessages: 7 })
    const state = JSON.stringify(jev.asked[0]!.state)
    expect(state).not.toContain('system-reminder')
    expect(state).toContain('Fix the failing test')
    expect(state.length).toBeLessThan(LOG.length)
  })

  test('a Jev failure throws (the hook turns it into a retryable skip)', async () => {
    const broken: JevAsker = { ask: async () => { throw new Error('503') } }
    let error: unknown
    try {
      await compact(session(), broken, { preserveRecentMessages: 7 })
    } catch (e) {
      error = e
    }
    expect(String(error)).toContain('503')
  })

  test('nothing to ask: no request, same transcript', async () => {
    const jev = fakeJev(() => 0.9)
    const short = [user('hi'), said('hello')]
    const result = await compact(short, jev)
    expect(jev.asked).toHaveLength(0)
    expect(result.messages).toEqual(short)
  })
})

describe('pieces', () => {
  test('decide: keep, trim, stub, and a trim that would cut nothing keeps', () => {
    const options = resolveOptions()
    const [call] = collectToolCalls([user('a'), use('x', 'Bash', {}), out('x', LOG), said('b')], 0)
    expect(decide(call!, 0.6, options).action).toBe('keep')
    expect(decide(call!, 0.4, options).action).toBe('trim')
    expect(decide(call!, 0.1, options).action).toBe('stub')
    const [small] = collectToolCalls([user('a'), use('y', 'Bash', {}), out('y', 'z'.repeat(900)), said('b')], 0)
    expect(decide(small!, 0.4, options).action).toBe('keep')
  })

  test('a partial read never supersedes, nor is superseded', () => {
    const calls = collectToolCalls([
      user('a'),
      use('p', 'Read', { file_path: '/f', offset: 10, limit: 5 }), out('p', 'x'),
      use('w', 'Read', { file_path: '/f' }), out('w', 'x'),
      use('p2', 'Read', { file_path: '/f', offset: 1 }), out('p2', 'x'),
      said('b'),
    ], 0)
    expect([...supersededCalls(calls)]).toEqual([])
  })

  test('batches split when the questions do not fit beside the state', () => {
    const calls = collectToolCalls(session(), 0).slice(0, 6)
    expect(batchCalls(calls, 27000, 28000).length).toBeGreaterThan(1)
    expect(batchCalls(calls, 1000, 28000)).toHaveLength(1)
  })

  test('trimText and shortenInput', () => {
    expect(trimText('abc', 10, 10)).toBe('abc')
    const cut = trimText('a'.repeat(50) + 'b'.repeat(50), 10, 5)
    expect(cut.startsWith('a'.repeat(10))).toBe(true)
    expect(cut.endsWith('b'.repeat(5))).toBe(true)
    expect(shortenInput({ a: 'short' }, 200)).toBeNull()
    expect(String(shortenInput({ a: 'q'.repeat(1000) }, 200)!.a).length).toBeLessThan(400)
  })

  test('goal and reminders', () => {
    expect(withoutReminders('<system-reminder>x\ny</system-reminder> do it')).toBe('do it')
    expect(goalOf([user('<system-reminder>z</system-reminder>')])).toBe('(no user prompt yet)')
  })

  test('parseResponse', () => {
    expect(parseResponse(200, true, '{"answers":{}}').answers).toEqual({})
    expect(() => parseResponse(500, false, 'boom')).toThrow('500')
    expect(() => parseResponse(200, true, 'nope')).toThrow('malformed')
    expect(() => parseResponse(200, true, '{}')).toThrow('missing answers')
  })

  test('maskSecrets keeps JSON valid and hides keys', () => {
    const raw = {
      a: 'key: sk-or-v1-fake0000aaaaaaaaaaaaaaaaaaaaaaaa and OPENROUTER_API_KEY=abcd1234efgh',
      b: 'curl -H "Authorization: Bearer abcdefghijklmnop1234" https://u:hunter22@host/x',
      c: 'ghp_abcdefghijklmnopqrstuvwxyz0123 then plain text stays',
    }
    const text = maskSecrets(JSON.stringify(raw))
    const back = JSON.parse(text) as typeof raw
    expect(text).not.toContain('fake0000')
    expect(text).not.toContain('abcd1234efgh')
    expect(text).not.toContain('abcdefghijklmnop1234')
    expect(text).not.toContain('hunter22')
    expect(text).not.toContain('ghp_abc')
    expect(back.c).toContain('plain text stays')
  })

  test('each question shows the call and its output, masked', async () => {
    const jev = fakeJev(() => 0.9)
    const leaky = session()
    leaky[4] = out('b1', 'deploying with TOKEN=supersecretvalue123\n' + LOG)
    await compact(leaky, jev, { preserveRecentMessages: 7 })
    const questions = JSON.stringify(jev.asked[0]!.questions)
    expect(questions).toContain('npm run build')
    expect(questions).toContain('deploying with TOKEN=[masked]')
    expect(questions).not.toContain('supersecretvalue123')
  })

  test('the state Jev reads is masked', async () => {
    const jev = fakeJev(() => 0.9)
    const leaky = session()
    leaky[0] = user('Fix it. My key is sk-or-v1-0123456789abcdef0123456789', 'h0')
    await compact(leaky, jev, { preserveRecentMessages: 7 })
    expect(JSON.stringify(jev.asked[0]!.state)).not.toContain('0123456789abcdef')
  })

  test('keyInEnvFile', () => {
    expect(keyInEnvFile('A=1\nOPENROUTER_API_KEY=sk-or-1\n')).toBe('sk-or-1')
    expect(keyInEnvFile('export OPENROUTER_API_KEY="sk-or-2"')).toBe('sk-or-2')
    expect(keyInEnvFile("OPENROUTER_API_KEY = 'sk-or-3' ")).toBe('sk-or-3')
    expect(keyInEnvFile('# OPENROUTER_API_KEY=x\nOPENROUTER_API_KEY=')).toBeUndefined()
  })
})

describe('cheap to get again', () => {
  const call = (tool: string, input: Record<string, unknown>) =>
    collectToolCalls([user('a'), use('x', tool, input), out('x', 'y'), said('b')], 0)[0]!
  test('reads, listings and searches are; anything that runs, writes or reaches out is not', () => {
    for (const command of ['cat a.txt | head -5', 'sed -n 1,20p x.py; grep -n "a\\|b" y.py', 'cd /tmp/claude-0 && ls -la',
      'git log --oneline -3', 'T=/x; grep -rn "x > y" $T', 'rg foo 2>/dev/null']) {
      expect([command, cheapToRedo(call('Bash', { command }))]).toEqual([command, true])
    }
    for (const command of ['echo x > a.txt', 'python3 - <<EOF\nprint(1)\nEOF', 'sed -i s/a/b/ f', 'npm test', 'git commit -m x',
      'curl https://x', 'rm -rf build', 'claude plugin test .', 'find . -delete', 'ls $(pwd)', 'env | grep KEY']) {
      expect([command, cheapToRedo(call('Bash', { command }))]).toEqual([command, false])
    }
    expect(cheapToRedo(call('Read', { file_path: '/a' }))).toBe(true)
    expect(cheapToRedo(call('WebFetch', { url: 'https://x' }))).toBe(false)
  })

  test('a file view Jev is unsure about is trimmed; a test run with the same score stays whole', () => {
    const options = resolveOptions()
    const read = collectToolCalls([user('a'), use('r', 'Bash', { command: 'cat big.py' }), out('r', LOG), said('b')], 0)[0]!
    const run = collectToolCalls([user('a'), use('t', 'Bash', { command: 'npm test' }), out('t', LOG), said('b')], 0)[0]!
    expect(decide(read, 0.55, options).action).toBe('trim')
    expect(decide(run, 0.55, options).action).toBe('keep')
    expect(decide(read, 0.4, options).action).toBe('stub')
    expect(decide(run, 0.4, options).action).toBe('trim')
  })
})

describe('notes name what they removed', () => {
  test('a stub lists the names, paths and values it held; a trim lists those of its middle', async () => {
    const body = 'def resolve_account(user_id):\n    path = "/srv/app/accounts.py"\n    limit_retries=17\n' + 'filler words here\n'.repeat(300)
    const ms = [user('fix it', 'h0'), use('x', 'Bash', { command: 'npm run build' }), out('x', body), said('ok'),
      user('a'), said('b'), user('c'), said('d'), user('e'), said('f'), user('g'), said('h')]
    const stubbed = await compact(ms, fakeJev(() => 0.1), { preserveRecentMessages: 8 })
    const note = byLabel(stubbed.messages, 'x').text
    expect(note).toContain('It held:')
    expect(note).toContain('resolve_account')
    expect(note).toContain('limit_retries=17')
    expect(note).toContain('Re-run the tool before relying on its details')
    expect(note).not.toContain('npm run build') // still shown in the call itself: not named again
    const middle = 'x'.repeat(1000) + '\nclass SessionLedger:\n' + 'y'.repeat(1000)
    expect(trimText(middle, 900, 400)).toContain('It held: SessionLedger')
    expect(trimText(middle, 900, 400, new Set(), new Set(['SessionLedger']))).not.toContain('It held') // shown elsewhere
  })
})

describe('dedupe', () => {
  const code = Array.from({ length: 60 }, (_, i) => `    const value_${i} = compute_${i}(input)`).join('\n')
  test('lines a newer output shows again fold out of the older one; the newer stays whole', async () => {
    const view = (from: number, to: number) => code.split('\n').slice(from, to).map((l, i) => `${from + i + 1}\t${l}`).join('\n')
    const ms = [
      user('fix it', 'h0'),
      use('v1', 'Bash', { command: 'sed -n 1,60p a.py' }), out('v1', view(0, 60)),
      use('v2', 'Read', { file_path: '/a.py', offset: 10, limit: 40 }), out('v2', view(10, 50)),
      said('ok'), user('go'), said('a'), user('b'), said('c'), user('d'), said('e'), user('f'), said('g'),
    ]
    const result = await compact(ms, fakeJev(() => 0.9), { preserveRecentMessages: 8 })
    const older = byLabel(result.messages, 'v1').text
    expect(older).toContain('40 lines shown again in a later output')
    expect(older).toContain('const value_0 = compute_0')   // only shown here: kept
    expect(older).not.toContain('const value_20 = compute_20') // shown again in v2: folded
    expect(byLabel(result.messages, 'v2').text).toBe(view(10, 50))
    expect(result.stats.folded).toBe(1)
  })

  test('short repeats and outputs that are too small to matter never fold', () => {
    expect(fold('}\n}\n}\nreturn x', new Set(['}', 'return x']))).toBeNull()
    expect(fold('a_long_line_that_repeats = 1', new Set(['a_long_line_that_repeats = 1']))).toBeNull() // saves < 300 chars
    expect(lineKey('  12→const x = 1')).toBe('const x = 1')
    expect(lineKey('src/a.py:12:def go():')).toBe('def go():')
    expect(lineKey('12:\tfoo')).toBe('foo')
  })
})

describe('budget mode', () => {
  const long = (n: number) => {
    const ms: Message[] = [user('build it', 'h0')]
    for (let i = 0; i < n; i++) ms.push(use(`c${i}`, 'Bash', { command: `npm run step${i}` }), out(`c${i}`, `step ${i} ok\n`.repeat(400)))
    for (let i = 0; i < 8; i++) ms.push(i % 2 ? said(`a${i}`) : user(`u${i}`))
    return ms
  }
  test('steps the least needed, oldest first, until the target fits; the newest keep longest', async () => {
    const plain = await compact(long(6), fakeJev(() => 0.9), { targetRatio: 0 })
    expect(plain.stats.budgetSteps).toBe(0)
    const tight = await compact(long(6), fakeJev(() => 0.9), { targetRatio: 0.6 })
    expect(tight.stats.budgetSteps).toBeGreaterThan(0)
    expect(tight.stats.charsAfter).toBeLessThan(plain.stats.charsAfter)
    expect(byLabel(tight.messages, 'c0').text.length).toBeLessThanOrEqual(byLabel(tight.messages, 'c5').text.length)
    expect(byLabel(tight.messages, 'c5').text).toBe('step 5 ok\n'.repeat(400)) // the newest, all else equal, stays whole
  })

  test('only long sessions get it by themselves', async () => {
    const short = await compact(long(6), fakeJev(() => 0.9))
    expect(short.stats.budgetSteps).toBe(0)
    const under = await compact(long(210), fakeJev(() => 0.9)) // ~305k tokens
    expect(under.stats.tokensBefore).toBeLessThan(LONG_SESSION_TOKENS)
    expect(under.stats.tokensBefore).toBeGreaterThan(280_000)
    expect(under.stats.budgetSteps).toBe(0)
    const over = await compact(long(250), fakeJev(() => 0.9)) // ~363k tokens
    expect(over.stats.tokensBefore).toBeGreaterThan(LONG_SESSION_TOKENS)
    expect(over.stats.budgetSteps).toBeGreaterThan(0)
  })
})

describe('images', () => {
  // Read's stored record for a screenshot, as Claude Code hands it to a compaction
  const shot = (w: number, h: number) => ({ type: 'image', file: { base64: 'iVBORw0KGgo'.repeat(50), type: 'image/png', dimensions: { displayWidth: w, displayHeight: h } } })
  const seen = (id: string, record: unknown): Message =>
    ({ role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: id, text: '', isError: false, result: record }], handle: `res-${id}` })
  const shots = (): Message[] => [
    user('Make the panel look right', 'h0'),
    use('s1', 'Read', { file_path: '/shots/before.png' }), seen('s1', shot(1600, 900)),
    use('s2', 'Read', { file_path: '/shots/after.png' }), seen('s2', shot(1600, 900)),
    said('Better now.'), user('go on'), said('ok'), user('go on'), said('ok'), user('go on'), said('ok'), user('go on'), said('ok'),
  ]

  test('sized from their dimensions, at most ~1.15 megapixels; an API block or MCP image counts too', () => {
    expect(imageTokensOf(shot(1000, 750))).toBe(1000)
    expect(imageTokensOf(shot(4000, 3000))).toBe(1534) // scaled down by the API
    expect(imageTokensOf({ content: [{ type: 'text', text: 'x' }, { type: 'image', source: { type: 'base64', data: 'abc' } }] })).toBe(1534)
    expect(imageTokensOf({ type: 'image', data: 'abc', mimeType: 'image/png' })).toBe(1534)
    expect(imageTokensOf({ type: 'text', text: 'no image' })).toBe(0)
    expect(imageTokensOf({ type: 'image', file: { base64: '' } })).toBe(0)
  })

  test('an old screenshot is asked about and becomes a note when done with; the counts include it', async () => {
    const jev = fakeJev((name) => (name === 'need_t1' ? 0.1 : 0.9))
    const result = await compact(shots(), jev)
    const question = JSON.stringify(jev.asked[0]!.questions)
    expect(question).toContain('an image (~1534 tokens)')
    const old = byLabel(result.messages, 's1')
    expect(old.result).toBeUndefined() // the image goes with the record
    expect(old.text).toContain('an image, ~1534 tokens')
    expect(old.text).toContain('Re-run the tool')
    expect(byLabel(result.messages, 's2').result).toEqual(shot(1600, 900)) // still needed: whole
    expect(result.stats.stubbed).toBe(1)
    expect(result.stats.tokensBefore - result.stats.tokensAfter).toBeGreaterThan(1400)
  })

  test('an image Jev is unsure about stays whole (it has no head and tail to keep)', async () => {
    const result = await compact(shots(), fakeJev(() => 0.5))
    expect(byLabel(result.messages, 's1').result).toEqual(shot(1600, 900))
    expect(result.stats.trimmed).toBe(0)
  })
})

test('a kept image beside an output that changed keeps its message whole (the engine rebuilds results from text)', async () => {
  const shot = { type: 'image', file: { base64: 'iVBORw0KGgo'.repeat(50), dimensions: { displayWidth: 1600, displayHeight: 900 } } }
  const both: Message = { role: 'user', text: '', toolUses: [], handle: 'res-pair', toolResults: [
    { tool_use_id: 'l1', text: 'step ok\n'.repeat(800), isError: false },
    { tool_use_id: 's1', text: '', isError: false, result: shot },
  ] }
  const messages: Message[] = [
    user('Make the panel look right', 'h0'),
    { role: 'assistant', text: '', toolUses: [{ tool_use_id: 'l1', tool: 'Bash', input: { command: 'npm run build' } }, { tool_use_id: 's1', tool: 'Read', input: { file_path: '/s.png' } }], handle: 'use-pair' },
    both,
    said('ok'), user('go on'), said('ok'), user('go on'), said('ok'), user('go on'), said('ok'), user('go on'), said('ok'),
  ]
  const result = await compact(messages, fakeJev((name) => (name === 'need_t1' ? 0.05 : 0.9)))
  expect(result.messages[2]).toBe(both) // the same object, its handle with it
})
