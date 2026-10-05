# Changelog

## 0.1.4 — 2026-10-05

- Windows support. File locking uses `msvcrt` where `fcntl` is missing; hook commands use forward-slash paths and
  the real Python interpreter (Claude Code runs hooks in Git Bash on Windows, where `python3` is often absent); all
  files and hook input/output are read and written as UTF-8 regardless of the system code page; state-file
  replacement retries while another hook has the file open; `ctx install`/`uninstall` recognize `Scripts\ctx.exe`
  hooks; Cowork sync paths use `/` and Cowork's session folder is recognized with `\` separators; the Cowork plugin
  picks `py -3` or `python` on Windows.
- `ctx install` writes Claude settings with LF line endings on every OS.
- CI runs on Windows, macOS and Linux; the release check now runs the installed hook command and the plugin's hook
  command through bash, as Claude Code does.

## 0.1.3 — 2026-10-05

- Coming back after a break no longer means `/clear`: the idle warning now suggests a new session in the same
  folder, which starts from the same digest and leaves the old chat in the session list.
- `ctx history`: lists the project's past conversations (they survive `/clear`); `ctx history N --open` renders one
  as a readable page, `--md` / `--json` for tools.
- In-app mod (`ctx install` puts it in `~/.claude/skills/ctxlc-bar`): a quiet status line, a hint above the prompt
  only when the cache went cold on a large chat or the context is ≥80% full (Start fresh / Continue here), and a
  `/ctxlc` pane with saved state, open issues and past conversations readable in place.
- `ctx status --session ID` reports one session's context and cache.
- Fix: the Cowork plugin also loads in local Claude Code sessions; beside `ctx install` hooks it ran every hook a
  second time and injected the digest twice (~2.4k tokens per session start). The plugin copy now stands down there.

## 0.1.2 — unreleased

- Cowork: output from its shell tools (`device_bash` on your computer, `mcp__workspace__bash` in the desktop VM) is
  now condensed and deduplicated like `Bash`, and noisy build/test commands get the same exit-code wrapper. MCP
  results too large to inline, which Claude Code saves to a file, are condensed from that file. Checked live
  with Claude Code 2.1.278 and a stand-in MCP shell tool: 73,941 → 538 and 19,140 → 535 characters, with the
  error line kept.

## 0.1.1 — unreleased

- Duplicate-output and unchanged-file notices no longer hide a result when Claude Code runs the same hook
  twice for one tool call (seen with CLI 2.1.286); records now carry the tool call ID.
- Version bump so Cowork replaces an already-uploaded 0.1.0 plugin instead of keeping it.

## 0.1.0 — unreleased

First public release.

- Hooks for Claude Code (`ctx install --project PATH` or `--global`, reversible with `ctx uninstall`): condense
  bulky Bash/Read output, keep requirements, decisions, the current task and open failures in
  `.claude/context/`, and re-inject them after `/clear`, compaction and model switches.
- PreCompact steers compaction summaries toward in-flight work; idle and model-switch guards warn before a
  full re-cache.
- `/ctx` skill and the `ctx` CLI (`note`, `task`, `resolve`, `handoff`, `state`, `search`, `show`, `report`, …).
- Cowork plugin (`ctx plugin`), with state sync between cloud tasks: by default each task syncs with its own
  project folder on the user's computer; `--sync-folder PATH` pins one folder, `--no-sync` turns it off.
- macOS and Linux only; on Windows `ctx install` refuses and hooks do nothing.
