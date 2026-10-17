import { describe, expect, test } from 'claude-code/testing'

import { NOTE_TAG, trimText } from '../src/apply.ts'
import { batchCalls, compact as compactActual, decide, reductionRatio, resolveOptions } from '../src/compact.ts'
import { imageTokensOf } from '../src/images.ts'
import { cheapToRedo } from '../src/kinds.ts'
import { LONG_SESSION_TOKENS } from '../src/budget.ts'
import { repeatText, expandRepeats } from '../src/dedupe.ts'
import { parseResponse } from '../src/openrouter.ts'
import { maskSecrets } from '../src/secrets.ts'
import { fitState, goalOf, withoutReminders } from '../src/state.ts'
import { collectToolCalls, supersededCalls } from '../src/transcript.ts'
import type { JevAsker, JevQuestions, JevState, Message } from '../src/types.ts'
import { keyInEnvFile } from '../hooks/register.ts'

// Scorer-focused tests disable the separate lossless repeat compressor.
const compact: typeof compactActual = (messages, asker, options) => compactActual(messages, asker, { dedupe: false, ...options })

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
    expect(byLabel(result.messages, 'b1').text).toContain(`${NOTE_TAG} removed Bash output`)
    expect(byLabel(result.messages, 't1').text).toBe(FAIL)                 // an error: pinned
    expect(byLabel(result.messages, 'r1').text).toContain('duplicate of tool_use_id="r2"')  // superseded by r2
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
    expect(names).not.toContain(`need_${label('r2')}`)
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

  test("a Write's historical input stays even when the current file could differ", async () => {
    const result = await compact(session(), fakeJev(() => 0.9), { preserveRecentMessages: 7 })
    const write = result.messages.flatMap((m) => m.toolUses).find((u) => u.tool_use_id === 'w1')!
    expect(write.input.file_path).toBe('/p/b.ts')
    expect(write.input.content).toBe('x'.repeat(5000))
  })

  test("old and recent script inputs stay whole", async () => {
    const script = 'python3 - <<EOF\n' + 'edit()\n'.repeat(1000) + 'EOF'
    const long = session()
    long[3] = use('b1', 'Bash', { command: script, description: 'apply the edits' })
    long[17] = use('t2', 'Bash', { command: script })
    const result = await compact(long, fakeJev(() => 0.9), { preserveRecentMessages: 7 })
    const old = result.messages.flatMap((m) => m.toolUses).find((u) => u.tool_use_id === 'b1')!
    expect(old.input.command).toBe(script)
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
  test('a session too long for the last stage leaves out its oldest entries instead of failing', async () => {
    const messages: Message[] = [user('Refactor everything')]
    for (let i = 0; i < 1500; i++) messages.push(use(`c${i}`, 'Bash', { command: `step ${i}` }), out(`c${i}`, LOG))
    messages.push(said('Done.'))
    const calls = collectToolCalls(messages, 8)
    const fitted = fitState(messages, calls, 'Refactor everything', 20000)
    expect(fitted.tokens).toBeLessThanOrEqual(20000)
    expect(fitted.omitted).toBeGreaterThan(0)
    const conversation = fitted.state.conversation as Record<string, unknown>[]
    expect(String(conversation[0]!.omitted)).toContain(`${fitted.omitted} earlier entries`)
    expect(JSON.stringify(conversation.at(-1))).toContain('assistant') // the newest stay
    expect(JSON.stringify(conversation.at(-2))).toContain('step 1499')
    const result = await compact(messages, fakeJev(() => 0.1))
    expect(result.stats.stubbed).toBeGreaterThan(0)
  })

  test('a state that fits leaves nothing out', () => {
    const messages = session()
    expect(fitState(messages, collectToolCalls(messages, 8), 'goal', 20000).omitted).toBe(0)
  })

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

  test('trimText', () => {
    expect(trimText('abc', 10, 10)).toBe('abc')
    const cut = trimText('a'.repeat(50) + 'b'.repeat(50), 10, 5)
    expect(cut.startsWith('a'.repeat(10))).toBe(true)
    expect(cut.endsWith('b'.repeat(5))).toBe(true)
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

  test('rerun cost does not alter relevance thresholds', () => {
    const options = resolveOptions()
    const read = collectToolCalls([user('a'), use('r', 'Bash', { command: 'cat big.py' }), out('r', LOG), said('b')], 0)[0]!
    const run = collectToolCalls([user('a'), use('t', 'Bash', { command: 'npm test' }), out('t', LOG), said('b')], 0)[0]!
    expect(decide(read, 0.55, options).action).toBe('keep')
    expect(decide(run, 0.55, options).action).toBe('keep')
    expect(decide(read, 0.4, options).action).toBe('trim')
    expect(decide(read, 0.2, options).action).toBe('stub')
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
    expect(note).toContain('Re-run it for details')
    expect(note).not.toContain('npm run build') // still shown in the call itself: not named again
    const middle = 'x'.repeat(1000) + '\nclass SessionLedger:\n' + 'y'.repeat(1000)
    expect(trimText(middle, 900, 400)).toContain('It held: SessionLedger')
    expect(trimText(middle, 900, 400, new Set(), new Set(['SessionLedger']))).not.toContain('It held') // shown elsewhere
  })
})

describe('lossless repeats and soft budget', () => {
  test('repeat compression preserves indentation, blank lines, counts and final newline', () => {
    for (const text of ['    value = 2.5 ms\n'.repeat(80), 'line\r\n'.repeat(80), 'a\n\n'.repeat(80)]) {
      expect(expandRepeats(repeatText(text))).toBe(text)
      expect(repeatText(repeatText(text))).toBe(repeatText(text))
    }
  })
  test('a hard-to-recover output scored needed survives an unmet target', async () => {
    const text = Array.from({length: 250}, (_, i) => `observation ${i}: ${i / 10} ms`).join('\n')
    const ms = [user('Keep the measured values'), use('v', 'Observe', {}), out('v', text)]
    const r = await compact(ms, fakeJev(() => 0.99), { preserveRecentMessages: 0, targetRatio: 0.1 })
    expect(byLabel(r.messages, 'v').text).toBe(text)
    expect(r.stats.budgetSteps).toBe(0)
    expect(r.stats.targetMet).toBe(false)
  })
  test('long-session targets are reported without overriding retention', async () => {
    const text = 'measured value 7.5 ms\n'.repeat(Math.ceil(LONG_SESSION_TOKENS * 3.2 / 20))
    const r = await compact([user('Keep it'), use('v', 'Observe', {}), out('v', text)], fakeJev(() => 0.99), {preserveRecentMessages: 0})
    expect(r.stats.targetRatio).toBe(0.35)
    expect(r.stats.targetMet).toBe(false)
    expect(byLabel(r.messages, 'v').text).toBe(text)
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
    expect(old.text).toContain('Re-run it')
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
