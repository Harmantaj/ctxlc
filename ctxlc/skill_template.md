---
name: ctx
description: Context manager control panel (ctxlc). Use when the user types /ctx, "/ctx <action>", or a message starting with "ctx <action>" (panel, reset, switch, handoff, digest, search, note kinds, resolve, report) — the panel's buttons send the slash-less form — smart reset, preparing a model switch, saving a handoff, searching old history, recording decisions.
---

# /ctx — ctxlc control panel

`CTX="__CTX__"` (always quote it: install paths can contain spaces).
Panel buttons send `ctx <action>` (no slash; widget prompts starting with "/" are dropped by the app), so treat `ctx X` exactly like `/ctx X`. If several arrive in one message, do each in order.
All commands act on the current working directory's project. Keep replies short: the panel is the UI.

## `/ctx` (no argument) or `ctx panel`: show the panel
1. Call `mcp__ccd_session_mgmt__get_usage` (load it with ToolSearch if deferred). If unavailable, use `{}`.
2. Save that JSON to a scratch file and run `"$CTX" panel --usage - < file`.
3. Pass the output unchanged as `widget_code` to the visualize `show_widget` tool (call its `read_me` with `["interactive"]` first if not yet loaded this session; title `ctx_control_panel`).
4. If no widget tool exists, print `"$CTX" status` instead and list the actions below.
Add one sentence at most after the panel, only if the recommendation needs action.

## Actions
- **`/ctx reset` — smart reset** (continue in a fresh, tiny context with exact state):
  1. Write a handoff: 5–15 lines a fresh context needs and the digest would not otherwise carry — what is in progress, what was just verified, the exact next step, open questions, gotchas learned this session. No history, no pleasantries. Save it: `"$CTX" handoff - <<'EOF' … EOF`.
  2. Record anything not yet saved: `"$CTX" task "…"`, `"$CTX" note decision "…"`, etc.
  3. `"$CTX" ingest` then `"$CTX" digest | head -40` to confirm the handoff is in it.
  4. Tell the user in one line: the context clears when this turn ends and the next session starts from ~N tokens of state (from `"$CTX" status --json` → `digest_tokens`).
  5. Call `mcp__ccd_session_mgmt__clear_session` with `session_id: "self"` as the LAST action. If refused or unavailable (e.g. CLI terminal), tell the user to type `/clear` — state is already saved, so that is safe.
- **`/ctx switch` — prepare a model switch**: same as reset, then tell the user to pick the new model in the model menu after the clear (the new model starts from the digest instead of re-caching the whole conversation). Do not call `set_session_model`.
- **`/ctx handoff`**: steps 1–3 of reset only; no clear. Use before stepping away for longer than the cache TTL.
- **`/ctx digest`**: run `"$CTX" digest` and show it in a code block.
- **`/ctx search <terms>`**: run `"$CTX" search "<terms>"`; summarize hits; use `"$CTX" show <ref>` for the one that answers the question.
- **`/ctx <kind> "<text>"`** where kind is decision|requirement|constraint|bug|note|config|issue: `"$CTX" note <kind> "<text>"`; for `task`: `"$CTX" task "<text>"`. Confirm with the new ID.
- **`/ctx resolve <ID>`**: `"$CTX" resolve <ID>`.
- **`/ctx history [N]`**: no N: run `"$CTX" history` and show the list. With N: run `"$CTX" history N --open` (opens that past conversation as a readable page) and say it opened. Past conversations survive `/clear`; this is how the user rereads one.
- **`/ctx report`**: run `"$CTX" report` and give the top-line numbers.
- **`/ctx refresh`** / **`ctx panel`**: same as no argument.

After any action except reset/switch, re-render the panel only if the user asked for it.

## Honest limits
- There is no tool to run `/compact`; if the user wants native compaction they type `/compact` themselves (ctxlc re-injects state afterwards automatically).
- `clear_session` needs the user's approval unless the session is in auto or bypass mode.
- Clearing hides the old chat in the app but never deletes it: it stays readable with `ctx history` (and the `/ctxlc` pane's Past conversations), and the app's "Resume previous session" brings it back. To keep it visible in the sidebar instead, the user starts a new session in this folder: state is restored there the same way.
