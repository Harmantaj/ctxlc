// ctxlc in the app. The hooks (`ctx install`) do the saving and restoring; this mod only makes it visible:
// a quiet status line, a hint above the prompt when starting fresh is cheaper than continuing, and a /ctxlc
// pane with the saved state and the project's past conversations (readable after /clear).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Conversation, CtxStatus, View } from '../types'

// Filled in by `ctx install`: the argv that runs this machine's ctx.
const CTX: readonly string[] = __CTX_ARGV__
const PANE = 'ctxlc'
const COLD_MIN_TOKENS = 60_000
const FULL_PERCENT = 80

const status = atom({ plugin: 'ctxlc-bar', key: 'status' } as const, null)
const context = atom({ plugin: 'ctxlc-bar', key: 'context' } as const, { tokens: null, percent: null })
const bandHidden = atom({ plugin: 'ctxlc-bar', key: 'bandHidden' } as const, false)
const conversations = atom({ plugin: 'ctxlc-bar', key: 'conversations' } as const, [])
const view = atom({ plugin: 'ctxlc-bar', key: 'view' } as const, { kind: 'home' })
const busy = atom({ plugin: 'ctxlc-bar', key: 'busy' } as const, null)

const k = (n: number | null | undefined) => (n == null ? '?' : n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)

function ago(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`
  return `${Math.round(seconds / 86400)}d`
}

function when(epoch: number): string {
  const d = new Date(epoch * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

type Advice = { tone: 'cold' | 'full'; text: string } | null

// When to speak up. Everything else stays in the status line.
function advice(s: CtxStatus | null, tokens: number | null, percent: number | null): Advice {
  if (!s) return null
  const size = tokens ?? s.context_tokens ?? 0
  if (!s.cache_warm && s.idle_s != null && size >= COLD_MIN_TOKENS) {
    return {
      tone: 'cold',
      text: `You were away ${ago(s.idle_s)}, so this chat's cache expired: the next message re-reads ~${k(size)} tokens. ` +
        `Starting fresh costs ~${k(s.digest_tokens)} and keeps your requirements, decisions and task. ` +
        `This chat stays readable under Past conversations in /ctxlc.`,
    }
  }
  if (percent != null && percent >= FULL_PERCENT) {
    return {
      tone: 'full',
      text: `Context is ${Math.round(percent)}% full. A fresh start now keeps exact state; waiting for auto-compaction keeps a summary.`,
    }
  }
  return null
}

async function ctx($: EngineInterface, args: string[]): Promise<string> {
  const r = await $.process.run([...CTX, ...args], { timeoutMs: 20_000 })
  if (r.exitCode !== 0) throw new Error((r.stderr || r.stdout).trim().slice(0, 200) || `ctx ${args[0]} failed`)
  return r.stdout
}

async function refresh($: EngineInterface) {
  try {
    // This session's own numbers: the project's latest session may be the old chat this one replaced.
    const s: CtxStatus = JSON.parse(await ctx($, ['status', '--json', '--session', await $.session.id()]))
    await update($, status, () => s)
    const u = await $.session.usage()
    const c = { tokens: u.context.tokens ?? null, percent: u.context.percent ?? null }
    await update($, context, () => c)
    const cache = s.idle_s == null ? 'new conversation' : s.cache_warm
      ? `cache warm${s.idle_s != null ? ` ${ago(Math.max(60, s.cache_ttl_s - s.idle_s))} left` : ''}`
      : 'cache cold'
    const saved = s.counts.requirements + s.counts.decisions
    const size = c.tokens ?? s.context_tokens
    $.ui.status(`ctxlc · ${size != null ? `${k(size)} context · ` : ''}${cache} · ${saved} rules saved · /ctxlc`)
  } catch (err) {
    $.ui.status(`ctxlc · unavailable (${String(err).slice(0, 60)})`)
  }
}

async function loadConversations($: EngineInterface) {
  try {
    const list: Conversation[] = JSON.parse(await ctx($, ['history', '--json', '--n', '12']))
    const id = await $.session.id()
    await update($, conversations, () => list.map(c => ({ ...c, current: c.id === id })))
  } catch {
    await update($, conversations, () => [])
  }
}

// Cold cache: state is saved at every reply, so a fresh start needs no model call (a handoff written now
// would re-read the whole cold context, the cost being avoided). Warm: let the model add a handoff first.
async function startFresh($: EngineInterface, tone: 'cold' | 'full') {
  await update($, busy, () => 'Saving state…')
  try {
    if (tone === 'cold') {
      await ctx($, ['ingest'])
      await update($, bandHidden, () => true)
      await $.command.run({ command: 'clear' })
    } else {
      await $.prompt.submit({ text: 'ctx reset' })
    }
  } finally {
    await update($, busy, () => null)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'ctxlc', description: 'Context manager: saved state, past conversations, fresh start' })
    void refresh($)
    $.clock.every(60_000, () => void refresh($))

    return next(e)
  })

  on('session.end', async ($, e, next) => {
    // A /clear: the next conversation starts with the band back in place.
    await update($, bandHidden, () => false)
    await update($, view, () => ({ kind: 'home' }) as View)

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    await update($, bandHidden, () => false)
    void refresh($)

    return done
  })

  on('command.run', { command: 'ctxlc' }, async $ => {
    await update($, view, () => ({ kind: 'home' }) as View)
    // Loaded before opening, so the pane never flashes an empty list (~0.3 s of local work).
    await Promise.all([refresh($), loadConversations($)])
    await $.ui.open({ id: PANE, title: 'Context' })

    return { text: 'Opened the ctxlc pane.' }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, status)
    const c = await read($, context)
    const a = advice(s, c.tokens, c.percent)
    if (!a || e.props.hasSurvey || (await read($, bandHidden))) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const working = await read($, busy)

    return (
      <Box flexDirection="column" gap={1}>
        <Text dimColor>{a.text}</Text>
        <Box flexDirection="row" gap={2} flexWrap="wrap">
          {working ? <Text dimColor>{working}</Text> : (
            <Button key="fresh" variant="primary" label="Start fresh" onPress={() => void startFresh($, a.tone)} />
          )}
          {a.tone === 'full' && <Button key="compact" label="Compact instead" onPress={() => void $.session.compact()} />}
          <Button key="pane" label="Past conversations" onPress={() => void $.command.run({ command: 'ctxlc' })} />
          <Button key="hide" role="dismiss" label="Continue here" onPress={() => void update($, bandHidden, () => true)} />
        </Box>
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text, Markdown } = $.ui.resolve(e)
    const v = await read($, view)
    if (v.kind === 'conversation') {
      return (
        <Box flexDirection="column" gap={1}>
          <Box flexDirection="row" gap={2}>
            <Button key="back" label="Back" onPress={() => void update($, view, () => ({ kind: 'home' }) as View)} />
            <Button key="web" label="Open as page" onPress={() => void ctx($, ['history', String(v.n), '--open'])} />
          </Box>
          <Text bold wrap="truncate-end">{v.title}</Text>
          <Markdown text={v.text} />
        </Box>
      )
    }
    const s = await read($, status)
    const c = await read($, context)
    const list = await read($, conversations)
    const a = advice(s, c.tokens, c.percent)
    if (!s) return <Text dimColor>Loading…</Text>
    const cache = s.idle_s == null ? 'new' : s.cache_warm ? 'warm' : 'cold'

    return (
      <Box flexDirection="column" gap={1}>
        <Text>
          {k(c.tokens ?? s.context_tokens)} context{c.percent != null ? ` (${Math.round(c.percent)}%)` : ''} · cache {cache} · fresh start ~{k(s.digest_tokens)}
        </Text>
        {s.task && <Text dimColor wrap="wrap">Task: {s.task}</Text>}
        <Text dimColor>
          Saved: {s.counts.requirements} requirements · {s.counts.decisions} decisions · {s.counts.open_bugs} open issues
        </Text>
        <Box flexDirection="row" gap={2} flexWrap="wrap">
          <Button key="fresh" variant={a ? 'primary' : undefined} label="Start fresh" onPress={() => void startFresh($, s.cache_warm ? 'full' : 'cold')} />
          <Button key="handoff" label="Save handoff" onPress={() => void $.prompt.submit({ text: 'ctx handoff' })} />
          <Button key="compact" label="Compact" onPress={() => void $.session.compact()} />
        </Box>
        {s.open_bugs.length > 0 && <Text bold>Open issues</Text>}
        {s.open_bugs.slice(0, 5).map(b => (
          <Box key={`bug-${b.id}`} flexDirection="row" gap={1}>
            <Text wrap="truncate-end">[{b.id}] {b.text}</Text>
            <Button key={`resolve-${b.id}`} label="Resolve" onPress={() => void ctx($, ['resolve', b.id]).then(() => refresh($))} />
          </Box>
        ))}
        <Text bold>Past conversations</Text>
        {list.length === 0 && <Text dimColor>None saved yet.</Text>}
        {list.map(conv => (
          <Button
            key={`conv-${conv.id}`}
            plain
            label={`${when(conv.end)}  ${conv.current ? '(this one) ' : ''}${conv.first.slice(0, 70)}`}
            onPress={async () => {
              const text = await ctx($, ['history', String(conv.n), '--md'])
              await update($, view, () => ({ kind: 'conversation', n: conv.n, title: conv.first.slice(0, 80), text }) as View)
            }}
          />
        ))}
      </Box>
    )
  })
}
