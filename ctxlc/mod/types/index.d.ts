export type CtxStatus = {
  session: string | null
  context_tokens: number | null
  idle_s: number | null
  cache_ttl_s: number
  cache_warm: boolean
  digest_tokens: number
  task: string | null
  counts: { requirements: number; decisions: number; open_bugs: number }
  open_bugs: { id: string; text: string }[]
}
export type Conversation = { n: number; id: string; end: number; prompts: number; first: string; current: boolean }
export type View = { kind: 'home' } | { kind: 'conversation'; n: number; title: string; text: string }

declare module 'claude-code' {
  interface PluginState {
    'ctxlc-bar': {
      status: CtxStatus | null
      context: { tokens: number | null; percent: number | null }
      bandHidden: boolean
      conversations: Conversation[]
      view: View
      busy: string | null
    }
  }
}
