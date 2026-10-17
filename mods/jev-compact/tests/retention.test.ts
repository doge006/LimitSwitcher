import { describe, expect, test } from 'claude-code/testing'
import { compact, mapBatches } from '../src/compact.ts'
import { expandRepeats, repeatText } from '../src/dedupe.ts'
import { withDeadline } from '../src/openrouter.ts'
import type { JevAsker, Message } from '../src/types.ts'

const user = (text: string): Message => ({ role: 'user', text, toolUses: [] })
const pair = (id: string, text: string, input: Record<string, unknown> = {}, tool = 'Read', error = false): Message[] => [
  { role: 'assistant', text: '', toolUses: [{ tool_use_id: id, tool, input }] },
  { role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: id, text, isError: error }] },
]
const result = (messages: Message[], id: string) => messages.flatMap(m => m.toolResults ?? []).find(r => r.tool_use_id === id)!.text
const asker = (probability: number): JevAsker => ({ ask: async (_, questions) => ({ answers: Object.fromEntries(Object.keys(questions).map(k => [k, { type: 'noul', noul: probability }])) }) })
const opts = { preserveRecentMessages: 0, targetRatio: 0.1 }
const view = (value: string) => Array.from({ length: 180 }, (_, i) => i === 90 ? `timeout = ${value} ms; applies only to uploads` : `item_${i} = observed_${i}`).join('\n')

describe('continuation evidence', () => {
  test('large repeated and blank runs round-trip without argument spreading limits', () => {
    for (const text of ['x\n'.repeat(150_000), '\n'.repeat(150_000)]) {
      expect(expandRepeats(repeatText(text))).toBe(text)
    }
  })
  test('historical values, cross-output relationship, intent, decision and unresolved error survive two passes', async () => {
    const original = [user('Compare the old upload timeout with the new one. Keep the download timeout unchanged.'),
      ...pair('before', view('2.5'), { file_path: '/config' }),
      ...pair('after', view('7.5'), { file_path: '/config' }),
      { role: 'assistant' as const, text: 'Uploads changed; download behavior still needs verification.', toolUses: [] },
      ...pair('error', 'download timeout is still 2.5 ms\n'.repeat(80), {}, 'Test', true)]
    let messages = original
    for (let i = 0; i < 2; i++) messages = (await compact(messages, asker(.99), opts)).messages
    expect(result(messages, 'before')).toContain('timeout = 2.5 ms; applies only to uploads')
    expect(result(messages, 'after')).toContain('timeout = 7.5 ms; applies only to uploads')
    expect(result(messages, 'error')).toBe(result(original, 'error'))
    expect(messages.map(m => m.text)).toEqual(original.map(m => m.text))
  })

  test('script and edit inputs remain the original recipe even when output is discarded', async () => {
    const command = 'padding '.repeat(100) + '\nx = 2.5; unit = "ms"; condition = "uploads only"\n' + 'padding '.repeat(100)
    const original = [user('Continue the experiment'), ...pair('script', view('7.5'), { command }, 'Bash'),
      ...pair('edit', 'ok', { file_path: '/config', old_string: command, new_string: 'new revision' }, 'Edit')]
    let messages = original
    for (let i = 0; i < 2; i++) messages = (await compact(messages, asker(.01), opts)).messages
    expect(messages.flatMap(m => m.toolUses)).toEqual(original.flatMap(m => m.toolUses))
  })

  test('exact duplicate pointers stay resolvable after later low scores', async () => {
    const original = [user('Continue'), ...pair('old', view('2.5'), { file_path: '/config' }), ...pair('source', view('2.5'), { file_path: '/config' })]
    const first = await compact(original, asker(.01), opts)
    expect(first.stats.asked).toBe(0)
    expect(result(first.messages, 'old')).toContain('duplicate of tool_use_id="source"')
    const second = await compact(first.messages, asker(.01), opts)
    expect(result(second.messages, 'source')).toBe(view('2.5'))
    expect(second.messages).toEqual(first.messages)
  })

  test('different source, indentation and order never count as duplicates', async () => {
    const text = view('7.5')
    const original = [user('Compare'), ...pair('a', text, { file_path: '/a' }), ...pair('b', text, { file_path: '/b' }),
      ...pair('c', text.replace(/^/gm, '    '), { file_path: '/a' }), ...pair('d', text.split('\n').reverse().join('\n'), { file_path: '/a' })]
    const r = await compact(original, asker(.99), opts)
    expect(r.stats.superseded).toBe(0)
    for (const id of ['a', 'b', 'c', 'd']) expect(result(r.messages, id)).toBe(result(original, id))
  })

  test('lossless compression saves requests and carries exact counts', async () => {
    const text = '    sample = 2.5 ms\n'.repeat(1000)
    const original = [user('Count the samples'), ...pair('samples', text)]
    const r = await compact(original, { ask: async () => { throw new Error('unnecessary scoring') } }, opts)
    expect(expandRepeats(result(r.messages, 'samples'))).toBe(text)
    expect(r.stats.charsAfter).toBeLessThan(r.stats.charsBefore / 10)
    expect((await compact(r.messages, asker(.01), opts)).messages).toEqual(r.messages)
  })

  test('an index that expands output is rejected and counted as retained', async () => {
    const text = Array.from({length: 40}, (_, i) => `datum_key_${i}`).join('\n')
    const r = await compact([user('Continue'), ...pair('values', text)], asker(.01), opts)
    expect(result(r.messages, 'values')).toBe(text)
    expect(r.stats.charsAfter).toBe(r.stats.charsBefore)
    expect(r.stats.stubbed).toBe(0)
  })

  test('low relevance scores still can lose unique unrecognized facts: the index is no archive', async () => {
    const text = view('2.5')
    const r = await compact([user('Unrelated work'), ...pair('values', text)], asker(.01), opts)
    expect(result(r.messages, 'values')).not.toContain('timeout = 2.5 ms; applies only to uploads')
  })
})

describe('scorer failure containment', () => {
  test('malformed, out-of-range and cross-batch answers fail before any replacement commits', async () => {
    const original = [user('Continue'), ...pair('a', view('2.5'))]
    const before = JSON.stringify(original)
    for (const bad of [-1, 2, NaN, Infinity, '0.1']) {
      await expect(compact(original, asker(bad as number), opts)).rejects.toThrow('malformed')
    }
    const extra: JevAsker = { ask: async () => ({ answers: { toString: {type: 'noul', noul: .1} } }) }
    await expect(compact(original, extra, opts)).rejects.toThrow('unexpected')
    const wrong: JevAsker = { ask: async () => ({answers: {need_t1: {type: 'other', noul: .1} as any}}) }
    await expect(compact(original, wrong, opts)).rejects.toThrow('malformed')
    expect(JSON.stringify(original)).toBe(before)
  })

  test('at most three requests run; a failure prevents queued batches', async () => {
    let active = 0, peak = 0
    const r = await mapBatches([1,2,3,4,5,6], async n => { active++; peak = Math.max(peak, active); await Promise.resolve(); active--; return n * 2 })
    expect(peak).toBe(3)
    expect(r).toEqual([2,4,6,8,10,12])
    let started = 0
    await expect(mapBatches([1,2,3,4,5,6], async () => { started++; throw new Error('offline') })).rejects.toThrow('offline')
    expect(started).toBe(3)
  })

  test('whole-job deadline rejects late results and prevents new transport work', async () => {
    let expire!: () => void, finish!: () => void
    let started = 0
    const network = new Promise<void>(resolve => { finish = resolve })
    const job = withDeadline(active => mapBatches([1,2,3,4,5,6], async () => {
      if (!active()) throw new Error('expired')
      started++
      await network
      if (!active()) throw new Error('expired')
      return 'late'
    }), callback => { expire = callback })
    const rejected = expect(job).rejects.toThrow('deadline exceeded')
    await Promise.resolve()
    expire()
    await rejected
    finish()
    await Promise.resolve()
    await Promise.resolve()
    expect(started).toBe(3)
  })
})
