import { describe, expect, mock, test } from 'claude-code/testing'
import type { On, SessionMessage } from 'claude-code'

import { FAILED, MARKER } from '../hooks/register.ts'

const user = (text: string, handle: string): SessionMessage => ({ role: 'user', text, toolUses: [], handle })
const use = (id: string, tool: string, input: Record<string, unknown>): SessionMessage =>
  ({ role: 'assistant', text: '', toolUses: [{ tool_use_id: id, tool, input }], handle: `use-${id}` })
const out = (id: string, text: string): SessionMessage =>
  ({ role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: id, text, isError: false }], handle: `res-${id}` })

const LOG = 'step ok\n'.repeat(800)

function transcript(): SessionMessage[] {
  return [
    user('Fix the build', 'h0'),
    use('b1', 'Bash', { command: 'npm run build' }), out('b1', LOG),
    use('b2', 'Bash', { command: 'npm run build' }), out('b2', LOG),
    user('more', 'h5'), user('more', 'h6'), user('more', 'h7'), user('more', 'h8'),
    user('more', 'h9'), user('more', 'h10'), user('more', 'h11'), user('more', 'h12'),
  ]
}

type World = { requests: { url: string; auth?: string; body: Record<string, unknown> }[]; core: string[] }

/** The engine beneath the mod: env, settings, files, a fake OpenRouter, and Claude Code's own compaction. */
function world(on: On, options: { env?: Record<string, string>; settingsKey?: string; file?: string; status?: number; need?: number } = {}): World {
  const w: World = { requests: [], core: [] }
  mock.env(on, options.env ?? {})
  on('settings.read', () => ({ value: options.settingsKey ? { env: { OPENROUTER_API_KEY: options.settingsKey } } : {} }))
  on('fs.read', ($, e) => {
    if (options.file === undefined) throw new Error('ENOENT')
    return { value: options.file }
  })
  on('ui.log', () => ({ value: undefined }))
  on('http.fetch', ($, e) => {
    const body = JSON.parse(e.init?.body ?? '{}') as Record<string, unknown>
    w.requests.push({ url: e.url, auth: e.init?.headers?.['authorization'], body })
    if ((options.status ?? 200) !== 200) return { value: { status: options.status!, ok: false, headers: {}, text: 'down' } }
    const answers = Object.fromEntries(Object.keys(body.questions as object).map((n) => [n, { type: 'noul', noul: options.need ?? 0.05 }]))
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ answers, usage: { cost: 0.0001 } }) } }
  })
  on('session.compact', ($, e) => {
    w.core.push(e.trigger)
    return { messages: [user('summary by Claude', 'sum')] }
  })
  return w
}

describe('jev-compact', () => {
  test("LimitSwitcher's request is pruned by Jev, never summarised by Claude", async ($, on) => {
    const w = world(on, { env: { OPENROUTER_API_KEY: 'sk-or-env' } })
    const result = await $.session.compact({ trigger: 'plugin', instructions: MARKER, messages: transcript() })

    expect(w.core).toEqual([])
    expect(w.requests).toHaveLength(1)
    expect(w.requests[0]!.url).toBe('https://openrouter.ai/api/alpha/decisions')
    expect(w.requests[0]!.auth).toBe('Bearer sk-or-env')
    expect(w.requests[0]!.body.model).toBe('typesafe/jev-1.13')
    expect(result.skip).toBeUndefined()
    expect(result.messages).toHaveLength(transcript().length)
    expect(result.messages![2]!.toolResults![0]!.text).toContain('[LimitSwitcher removed Bash output')
    expect(result.messages![0]!.handle).toBe('h0')
    if (result.skip !== undefined) throw new Error(result.skip)
    expect(result.tokensAfter!).toBeLessThan(result.tokensBefore!)
  })

  test('/compact and auto-compaction stay Claude Code\'s own by default', async ($, on) => {
    const w = world(on, { env: { OPENROUTER_API_KEY: 'sk-or-env' } })
    await $.session.compact({ trigger: 'manual', messages: transcript() })
    await $.session.compact({ trigger: 'auto', messages: transcript() })
    await $.session.compact({ trigger: 'plugin', instructions: 'something else', messages: transcript() })
    expect(w.requests).toEqual([])
    expect(w.core).toEqual(['manual', 'auto', 'plugin'])
  })


  test('the key comes from settings.json, then the .env file', { options: { envFile: '/data/.env' } }, async ($, on) => {
    const w = world(on, { settingsKey: 'sk-or-settings', file: 'OPENROUTER_API_KEY=sk-or-file\n' })
    await $.session.compact({ trigger: 'plugin', instructions: MARKER, messages: transcript() })
    expect(w.requests[0]!.auth).toBe('Bearer sk-or-settings')
  })

  test('the .env file alone', { options: { envFile: '/data/.env' } }, async ($, on) => {
    const w = world(on, { file: 'OPENROUTER_API_KEY=sk-or-file\n' })
    await $.session.compact({ trigger: 'plugin', instructions: MARKER, messages: transcript() })
    expect(w.requests[0]!.auth).toBe('Bearer sk-or-file')
  })

  test('`/compact limitswitcher:jev-compact` typed by hand prunes too', async ($, on) => {
    const w = world(on, { env: { OPENROUTER_API_KEY: 'sk-or-env' } })
    const result = await $.session.compact({ trigger: 'manual', instructions: MARKER, messages: transcript() })
    expect(w.core).toEqual([])
    expect(result.messages![2]!.toolResults![0]!.text).toContain('[LimitSwitcher removed')
  })

  test('no key: a final skip, and Claude is never asked', async ($, on) => {
    const w = world(on)
    const result = await $.session.compact({ trigger: 'plugin', instructions: MARKER, messages: transcript() })
    expect(w.core).toEqual([])
    expect(result.skip).toContain('no OpenRouter key')
    expect(result.skip!.startsWith(FAILED)).toBe(false)
  })

  test('Jev down: a retryable skip, and Claude is never asked', async ($, on) => {
    const w = world(on, { env: { OPENROUTER_API_KEY: 'k' }, status: 503 })
    const result = await $.session.compact({ trigger: 'plugin', instructions: MARKER, messages: transcript() })
    expect(w.core).toEqual([])
    expect(result.skip!.startsWith(FAILED)).toBe(true)
    expect(result.skip).toContain('503')
  })

  test('nothing worth pruning: a final skip', async ($, on) => {
    const w = world(on, { env: { OPENROUTER_API_KEY: 'k' }, need: 0.95 })
    const result = await $.session.compact({ trigger: 'plugin', instructions: MARKER, messages: transcript() })
    expect(w.core).toEqual([])
    expect(result.skip).toContain('nothing worth pruning')
    expect(result.skip!.startsWith(FAILED)).toBe(false)
  })
})
