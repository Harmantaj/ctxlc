// `claude plugin test ctxlc/mod` (after `ctx install` filled in __CTX_ARGV__, or on an installed copy).
// ctx itself is mocked: these tests cover what the mod shows and does with ctx's answers.
import { expect, mock, test } from 'claude-code/testing'

const STATUS = (over: Record<string, unknown>) => JSON.stringify({
  session: 'sess-1', context_tokens: 180_000, idle_s: 3 * 3600, cache_ttl_s: 3600, cache_warm: false,
  digest_tokens: 2400, task: 'ship 0.2', counts: { requirements: 9, decisions: 12, open_bugs: 1 },
  open_bugs: [{ id: 'F3', text: 'make test fails' }], ...over,
})
const HISTORY = JSON.stringify([
  { n: 1, id: 'sess-1', end: 1791000000, prompts: 4, first: 'continue the parser', current: false },
  { n: 2, id: 'old-2', end: 1790900000, prompts: 9, first: 'build the parser', current: false },
])

type World = { status: string; ran: string[][]; statusLine: (string | undefined)[]; commands: string[]; prompts: string[] }

function world(on: any, over: Record<string, unknown> = {}, percent = 40): World {
  const w: World = { status: STATUS(over), ran: [], statusLine: [], commands: [], prompts: [] }
  mock.clock(on)
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: 180_000, window: 1_000_000, percent }, rateLimits: [] } }))
  on('process.run', ($: unknown, e: { argv: string[] }) => {
    const args = e.argv.slice(e.argv.indexOf('status') >= 0 ? e.argv.indexOf('status') : e.argv.findIndex(a => ['history', 'ingest', 'resolve'].includes(a)))
    w.ran.push(args)
    const out = args[0] === 'status' ? w.status : args[0] === 'history' && args.includes('--json') ? HISTORY
      : args[0] === 'history' ? '**You** · 10/1 09:00\n\nbuild the parser' : ''
    return { value: { exitCode: 0, stdout: out, stderr: '' } }
  })
  on('ui.status', ($: unknown, e: { text: string | undefined }) => { w.statusLine.push(e.text); return { value: undefined } })
  on('command.run', ($: unknown, e: { command: string }) => { w.commands.push(e.command); return { text: '' } })
  on('prompt.submit', ($: unknown, e: { text: string }) => { w.prompts.push(e.text); return { text: e.text } })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', () => ({ cwd: '/proj' }))
  on('command.register', () => ({ value: { command: 'ctxlc' } }))
  // The engine's own drawing, under the mod's (what a band shows when the mod passes).
  on('ui.render', ($: any, e: any) => { const { Box } = $.ui.resolve(e); return <Box key="engine" /> })
  return w
}

// session.start fires ctxlc's first refresh; it runs unawaited, so let it settle.
async function started($: any, on: any, over: Record<string, unknown> = {}, percent = 40): Promise<World> {
  const w = world(on, over, percent)
  await $.session.start({ cwd: '/proj' } as never)
  for (let i = 0; i < 20 && w.statusLine.length === 0; i++) await new Promise(r => setTimeout(r, 10))
  return w
}

// What a drawing shows: its Text, Markdown and Button labels.
const shownIn = async (ui: any) => (await ui.findAll({})).map((el: any) => [el.text, el.props?.label, el.props?.text].filter(Boolean).join(' ')).join(' | ')

const BAND = { hasSurvey: false, isWorking: false, maxRows: 12, columns: 100 }

test('cold cache after a break: hint offers a fresh start that clears without a model call', async ($, on) => {
  const w = await started($, on)
  const ui = await $.ui.mount({ plugin: 'ctxlc-bar', surface: 'desktop', component: 'AbovePrompt', props: BAND as never })
  const text = (await ui.findAll({ type: 'Text' })).map(t => t.text).join(' ')
  expect(text).toContain('You were away 3.0h')
  expect(text).toContain('~180k tokens')
  expect(w.statusLine.at(-1)).toContain('cache cold')
  await ui.press({ key: 'fresh' })
  expect(w.ran.some(a => a[0] === 'ingest')).toBe(true)
  expect(w.commands).toContain('clear')
  expect(w.prompts).toEqual([])
})

test('warm, small context: no hint, quiet status line', async ($, on) => {
  const w = await started($, on, { idle_s: 120, cache_warm: true, context_tokens: 30_000 }, 10)
  const ui = await $.ui.mount({ plugin: 'ctxlc-bar', surface: 'desktop', component: 'AbovePrompt', props: BAND as never })
  expect(await ui.find({ key: 'fresh' })).toBeUndefined()
  expect(w.statusLine.at(-1)).toContain('cache warm 58m left')
})

test('nearly full: hint offers a reset that lets the model write a handoff, or compaction', async ($, on) => {
  const w = await started($, on, { idle_s: 60, cache_warm: true }, 85)
  const ui = await $.ui.mount({ plugin: 'ctxlc-bar', surface: 'desktop', component: 'AbovePrompt', props: BAND as never })
  expect(await ui.find({ key: 'compact' })).toBeDefined()
  await ui.press({ key: 'fresh' })
  expect(w.prompts).toEqual(['ctx reset'])
  expect(w.commands).not.toContain('clear')
})

test('pane lists past conversations and reads one in place', async ($, on) => {
  const w = await started($, on)
  await $.command.run({ command: 'ctxlc' })
  const ui = await $.ui.mount({ plugin: 'ctxlc-bar', surface: 'desktop', component: 'Pane', requestId: 'ctxlc', props: { title: 'Context', isFocused: true, bodyColumns: 80 } as never })
  const shown = await shownIn(ui)
  expect(shown).toContain('Task: ship 0.2')
  expect(shown).toContain('(this one) continue the parser')
  await ui.press({ key: 'conv-old-2' })
  expect(await ui.find({ key: 'back' })).toBeDefined()
  expect(await shownIn(ui)).toContain('build the parser')
  expect(w.ran.some(a => a[0] === 'history' && a.includes('--md'))).toBe(true)
})
