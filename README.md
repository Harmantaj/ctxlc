# ctxlc — context lifecycle manager for Claude Code

Keeps long Claude Code sessions cheap without making the model forget the project.
Pure Python 3 standard library, no model calls, installed as Claude Code hooks.

**New here? Read the [complete overview](docs/OVERVIEW.md)**: what ctxlc does, what you see in the app, everyday
situations, and measured results, in plain language. This README is the detailed reference.

```bash
pipx install git+https://github.com/Harmantaj/ctxlc.git   # or: pip install --user git+https://github.com/Harmantaj/ctxlc.git (Python 3.9+, macOS or Linux)
ctx install --project /path/to/project        # writes .claude/settings.local.json (reversible: ctx uninstall …)
ctx install --global                          # or for every project (~/.claude/settings.json)
```

macOS and Linux only: ctxlc locks its state files with `fcntl`, so `ctx install` refuses to run on Windows. To install
from a source checkout instead, use `pipx install /path/to/ctxlc`.

From a source checkout, `bin/ctx` works the same without installing. Hooks and the `/ctx` skill call `ctx` by
the absolute path of the copy that ran `install` (the checkout's `bin/ctx`, or the console script in the pipx
environment), because hooks may run without `~/.local/bin` on `PATH`. After moving or reinstalling ctxlc into a
different environment, run `ctx install` again.

Install backs up the settings file to `<file>.ctxlc.bak`, refuses to touch a settings file that is not valid
JSON, and writes the `/ctx` skill to `~/.claude/skills/ctx/SKILL.md` (`--no-skill` to skip). Start a new session
afterwards. `uninstall` removes only ctxlc's hooks (and the skill with `--global`); `.claude/context/` is kept.

## Where it works

| surface | status | how it was checked |
|---|---|---|
| Claude Code CLI (`claude`) | works | live `claude -p` sessions, benchmarks below |
| Claude desktop app, Code tab | works | this project was developed in it; every session start shows the injected digest |
| VS Code extension (`anthropic.claude-code` 2.1.276) | works | the extension's bundled binary run against a scratch project: it fired startup, PreCompact and compact re-injection, and a fresh session quoted the stored requirement back. The extension reads the same `~/.claude/settings.json`, so `ctx install --global` covers it; the VS Code chat panel itself was not driven by hand |
| Cowork (desktop app and claude.ai) | **plugin: works within a task; between tasks per project folder** (see below) | Cowork ignores `~/.claude/settings.json`, so `ctx install` does nothing there; upload the plugin instead. Tested 2026-10-04 in Cowork on claude.ai: the startup hook injected the protocol, and the rule stated in the first message was recorded in `/home/claude/.claude/context/state.json`. State does not carry over to a new task (each task starts with an empty `/home/claude`). `/compact` could not be sent from the web composer, so compact re-injection inside Cowork is untested there (it works in the CLI) |
| claude.ai chat / mobile | **not supported** | no hooks and no local filesystem. Closest substitute: `ctx digest \| pbcopy` and paste it, or keep it in a Project's instructions |

### Cowork

```bash
bin/ctx plugin --out ctxlc-plugin.zip         # builds a Claude plugin with the same hooks
```

A ready-built `ctxlc-plugin.zip` is attached to each [GitHub release](https://github.com/Harmantaj/ctxlc/releases/latest). With a pip install, run `ctx plugin` instead of
`bin/ctx plugin`.
Upload `ctxlc-plugin.zip` in the Claude app under **Customize → Plugins**, then start a new Cowork task. When you
upload a rebuilt zip, its version must be higher than the installed one (check **Contents** on the plugin page),
or Cowork keeps the old copy. A claude.ai plugin also loads in local Claude Code sessions; since 0.1.3 its hooks
stand down there when `ctx install` hooks are present, so nothing runs twice.

Cowork runs plugin hooks (verified 2026-10-04 on claude.ai, despite older reports such as
[#47993](https://github.com/anthropics/claude-code/issues/47993)). Tasks run in a cloud sandbox with cwd `/home/claude`,
so state lives in `/home/claude/.claude/context/` for the life of one task; a new task starts empty. To check a task,
ask it to run `tail -3 .claude/context/metrics.jsonl`: an `inject` line means the hooks ran.

What differs in Cowork:
* State lives in the task's own `.claude/context/`, never in `outputs/` (the folder you get back).
* The plugin ships no `ctx` executable (claude.ai rejects a top-level `bin/`), so injected commands run the
  plugin's copy as `PYTHONPATH="<plugin dir>" python3 -m ctxlc …`.
* Cowork's shell tools are MCP tools (`device_bash`, which runs on your computer, and `mcp__workspace__bash` in the
  desktop VM), not `Bash`. Since 0.1.2 their output is condensed and deduplicated the same way, including results
  Claude Code saved to a file for being too large. Read deduplication applies as before.

#### Keeping state between Cowork tasks (one state file per project folder)

```bash
bin/ctx plugin --out ctxlc-plugin.zip                              # default: each task's own project folder
bin/ctx plugin --out ctxlc-plugin.zip --sync-folder "$HOME/Claude" # always this one folder
bin/ctx plugin --out ctxlc-plugin.zip --no-sync                    # state lasts one task
```

The cloud sandbox cannot mount a folder from your computer (checked 2026-10-04: only a per-task `outputs` mount
exists). Only the model's remote-device tools can reach the folder, and hooks cannot call tools, so the hooks have the
model copy the state:
* At the start of a task, the injected instructions tell the model to pick the folder on your computer the task works
  in (the connected folder, or the one your request names), `cat <folder>/.claude/ctxlc-cowork.json` there and pipe it
  into `ctx sync-import --folder <folder>`. That merges it into the task and prints the restored state. A task with no
  folder runs `ctx sync-import --off` and is not synced; the model is told not to request folder access only for ctxlc.
* When the durable state changes (requirements, decisions, task, brief, handoff, open failures, but not every
  message), the Stop hook asks the model, once per change, to write the new export back to that folder's file.

So every Cowork project that works in its own folder keeps its own state, and nothing leaks between projects.

Trade-offs: each copy goes through the model, so it costs tokens. That is a few thousand tokens for a large project,
paid at task start and on each durable change. Saving depends on the model following the instructions, and the
hooks cannot confirm the write happened. The folder must be attached to the task with the computer online; folder
access is granted per task, so approve the request when a new task asks. Nothing is saved until the task has pulled
the file or confirmed it does not exist, so a task that skipped the pull cannot overwrite the saved state. Two tasks
running at once in the same folder each overwrite the file with their own state. A folder connected only after the
first message is not synced in that task.

Tested with unit tests and a simulation of the packaged zip: four tasks over two project folders, where each folder
restored only its own rules. In real Cowork tasks (2026-10-04) the fixed-folder mode saved the state, and a fresh
task requested folder access by itself and restored the saved rule. The per-folder default has not yet been run in a
real Cowork task.

## Day to day: what happens on `/clear`, `/compact` and auto-compaction

You don't run anything. ctxlc saves continuously; Claude Code's own `/clear` and compaction just become safe.

* **Saving** happens at every `Stop` (end of each reply), `PreCompact` and `SessionEnd`: the new part of the
  transcript is read and requirements, decisions, the current task, open failures, files and recent messages are
  updated in `.claude/context/state.json`. Nothing depends on Claude remembering to take notes.
* **`/clear`** starts a session with no conversation. ctxlc's `SessionStart` hook injects the digest (≈2k tokens),
  so the next answer already knows the requirements, decisions and the task. Older detail is one
  `ctx search` away. (The desktop app's clear arrives as a `startup` session; it is restored the same way.)
* **`/compact` and auto-compaction** (ctxlc leaves the trigger point alone, so a 1M-context model keeps its whole
  window; `ctx install --window 300000` opts into earlier compaction):
  1. `PreCompact` saves the latest state, then hands Claude's summarizer the list of what ctxlc will restore
     (`R3 (requirement): …`, `D2 (decision): …`, `F4 (open failure): …`) and asks it to cite those by ID and
     spend the summary on what ctxlc can't see: the step in progress, approaches ruled out and why, files being
     changed, the next action. Checked live: Haiku's summary referred to "R1" and "D1" instead of restating them.
  2. `PostCompact` archives Claude's summary into searchable history.
  3. `SessionStart(compact)` re-injects the exact digest, so rules survive word for word even if the summary
     paraphrased or dropped them.
* **Coming back after a break:** you don't have to clear. Start a **new session in the same folder**: it starts
  from the same ≈2k-token digest as `/clear`, and the old chat stays in the session list where you can read it.
  `/clear` costs the same but hides the old chat in the app; nothing is deleted either way. Every past
  conversation stays readable with `ctx history` (list) and `ctx history N --open` (a readable page), or in the
  `/ctxlc` pane. If you just send a message in the old chat after the prompt cache expired, ctxlc holds that first
  prompt once and says what continuing would cost; sending it again goes ahead.
* **Which to use:** a fresh start (new session or `/clear`) when switching to new work or after a break: it's
  the cheapest (the digest replaces the whole history; in the continuation benchmark below, ctxlc's digest cost
  $0.020 per continuation against $0.507 for `/compact`, both scoring 13/13). Use `/compact` mid-task, when the
  in-flight reasoning matters.

### In the app: status line, hint and `/ctxlc` pane

`ctx install` also installs a small in-app mod (`~/.claude/skills/ctxlc-bar`), so ctxlc is visible without you
asking for it:

* **Status line**, always there and quiet: `ctxlc · 123k context · cache warm 48m left · 21 rules saved · /ctxlc`.
* **A hint above the prompt, only when it pays to act:** when you come back to a large chat whose cache expired
  (≥60k tokens), or when the context is ≥80% full. **Start fresh** saves state and clears in one click; when the
  cache is cold it writes no model handoff, because that would re-read the whole cold context. **Continue here**
  dismisses it.
* **`/ctxlc` pane:** context size and cache, the current task, saved requirements/decisions, open issues (with
  Resolve), Start fresh / Save handoff / Compact, and **Past conversations**: pick one to read it inside the pane,
  or open it as a page.

The `ctx` skill's chat panel (`ctx panel`) still works; the mod replaces it for everyday use.

## What actually costs tokens (measured on this machine's 33 real sessions, 4,408 requests)

| finding | number |
|---|---|
| Share of input tokens that are **cache reads** (whole context re-read on every request) | 96.9 % |
| Peak context of the largest sessions | 425k – 597k tokens |
| Share of **cache writes** caused by full-context rebuilds (idle > cache TTL, model switch) | 77.4 % |
| Example model switch (Fable → Opus at 306k context) | 306,736-token cache write |
| Example resume after 8 h idle | 481,241-token cache write |
| Compactions in all history | 2 (both manual) |
| Fixed prefix (system prompt + tools) per session | 16k – 68k tokens |

So cost ≈ *context size × number of requests* (reads) + *full rebuilds after idle or switch* (writes).
Summaries being short is irrelevant unless one of those two terms shrinks.

## What Claude Code lets us control (verified in the 2.1.278 binary and by live probes)

| lever | used for |
|---|---|
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | opt-in only (`--window N`): trigger native compaction earlier. Not set by default, because a fixed cap took context away from large projects on 1M models |
| `PostToolUse` → `updatedToolOutput` (all tools; must keep the tool's output shape) | compact / dedup Bash and Read output before the model sees it |
| `PreToolUse` → `updatedInput` (no `permissionDecision`, so permissions are unaffected) | let failing build/test output reach the extractor |
| `SessionStart` (`startup`/`clear`/`compact`/`resume`) → `additionalContext` | inject the durable-state digest |
| `PreCompact` → plain stdout on exit 0 is appended to the compaction instructions | tell the summary which state ctxlc restores (by ID), so it spends its space on in-flight work |
| `PostCompact` (receives the summary) | archive the summary into searchable history |
| `PreModelSwitch` (`context_tokens`, `prompt_cache_warm`, `estimated_cache_write_usd`) → `ask` | warn before a switch forfeits a large warm cache |
| `UserPromptSubmit` → `decision: block` | warn once before an idle resume re-caches a huge cold context |
| `Stop` / `SessionEnd` | incremental ingestion of the transcript |

Not controllable: hooks cannot delete or edit past messages; only compaction or `/clear` shrinks the live context.
A failing command fires `PostToolUseFailure`, which cannot rewrite output. Hooks cannot run `/compact` or `/clear` themselves.
Cache TTL, pricing, and the system-prompt/tool prefix are owned by Claude Code and the API.

## Architecture

```
            Claude Code session (L0: live context, owned by Claude Code)
   ┌──────────────┬──────────────────┬───────────────────┬────────────────┐
PreToolUse     PostToolUse        Stop/PreCompact      SessionStart     PreModelSwitch /
(wrap noisy    (L1: extract,      /SessionEnd          (inject L2       UserPromptSubmit
 commands)      dedup, archive)   (incremental ingest)  digest)          (cold-cache guards)
                     │                   │                  ▲
                     ▼                   ▼                  │
      .claude/context/artifacts/   state.json (L2) ──render(budget)
            (L3)                   history.jsonl (L3) ◄── bin/ctx search / show
```

* **L1 — tool output at ingestion.** Bash output over 6,000 chars is replaced by an extract (head, every
  error/warning/summary line with context, tail, repeated lines folded, diffs as per-file stats) plus the path of
  the full copy. Identical output repeated in one context window becomes a pointer; re-reading an unchanged file
  range becomes a pointer. Nothing is lost: full text stays on disk. Commands that only display content
  (`cat`, `head`, `tail`, `sed -n`, `nl`, `grep`/`rg`, `jq`, `git show|diff|log|blame`, alone or piped together)
  pass through unchanged up to 30,000 chars: an error-line extract of source code is useless and forces a
  second read. Test/build/install commands get an
  exit-code-preserving suffix so that *failing* output can be extracted too (otherwise Claude Code shows only the
  first ~10k chars, usually missing the error).
* **L2 — durable project state**, maintained deterministically from the transcript delta (byte cursor per
  transcript; cost ∝ new activity): user prompts, the defining brief, directive sentences from the user
  ("must…", "never…", mid-sentence "keep it out of…", first-person "I can't stand / I'd rather …"; quoted text
  ignored), edited/read files, todos, failing commands (including `cmd > log 2>&1; echo "exit=$?"` runs, with error lines from later reads of
  that log merged in; auto-resolved when the same command, or a
  broader run of it, later succeeds — `npm test` passing resolves `npm test -- billing.spec`; permission denials
  excluded; a pipe that hides the exit code — `make test | tail` — still counts when the output says it failed, and
  follow-up runs of the same command add their error lines to the open failure; the parts the user says they
  fixed — "I patched the unicode bug" — are closed and listed as user-reported fixes), the last assistant status. The model adds semantic items with
  one-line commands (`ctx note decision "…" --supersedes D2`, `ctx task …`, `ctx resolve B1`). Items have
  status `active / superseded / resolved / done / archived`; near-duplicate items of the same kind supersede
  automatically, and a new task replaces the old one. Rendered into a priority-ordered digest under a token
  budget (2,500 default): task → handoff (shown to the next session only — and across that session's
  compactions — or to any session starting within 6 h of it) → brief → requirements/constraints → decisions → open bugs → config → last
  status → recent messages → recently modified files (paths only; source files are the source of truth).
* **L3 — recoverable history**: every prompt, assistant message, tool call, compaction summary and archived
  output, searchable locally with BM25 (`ctx search`), including superseded decisions (clearly labelled).
* **Triggers** (chosen by replaying real traces, `bench/simulate.py`): state restore when the cache is cold and
  the context is over 60k. Compaction stays at Claude Code's default point; `ctx install --window N` lowers it
  (the replay favoured 160k on cost, but a fixed cap would throw away most of a 1M-context window on big
  projects). Windows ≤ 80k backfire because the fixed prefix is 16–68k.

### Same model, long-running (hours → days)
L1 slows growth; auto-compaction (optionally earlier with `--window`) caps per-request reads; after compaction `SessionStart(compact)`
re-injects the digest so exact requirements/decisions survive the lossy summary. When you come back after the
cache expired, the first prompt is held once with the cost (e.g. "idle 15.2h … re-caches ~480k tokens"):
`/clear` restores state from L2 for ~2k tokens, or resend to keep full history.

### Model switch
`PreModelSwitch` asks before discarding a warm cache over 60k tokens and names the cheaper path
(`/compact` then switch, or `/clear` then switch — state is re-injected on the new model).

## Commands

| command | purpose |
|---|---|
| `ctx note <kind> "<text>" [--supersedes ID…]` | record requirement / constraint / decision / bug / issue / config / note |
| `ctx task "<text>"` · `ctx resolve <ID>` | set current task · close a bug/issue |
| `ctx digest` · `ctx state [--all]` | what gets injected · every item incl. superseded |
| `ctx search <terms>` · `ctx show <ref> [--grep RE] [--lines a:b]` | L3 retrieval (`hN`, `u:<uuid>`, `D3`, `a:<hash>`) |
| `ctx report` | actual API usage per session (from transcripts) + ctxlc savings |
| `ctx history` · `ctx history N --open` · `--md` · `--json` | past conversations of this project (kept after `/clear`), newest first · open one as a readable page · as Markdown · as JSON |
| `ctx handoff "<text>"\|-` · `ctx status [--json]` · `ctx panel [--usage JSON\|-]` | save a handoff for the next context · summary · HTML for the `/ctx` panel |
| `/ctx` (skill in `~/.claude/skills/ctx`) | in-app panel; buttons: smart reset, prepare model switch, save handoff, digest, search, note, resolve |
| `.claude/context/config.json` | override any value in `ctxlc/config.py` (thresholds, guards on/off) |

## Results

Reproduce: `python3 -m unittest tests.test_ctxlc tests.test_fixtures` (73 tests; the fixtures are synthetic transcripts
regenerated by `tests/fixtures/make_fixtures.py`) · `python3 tests/e2e.py [transcript.jsonl]` (29 end-to-end checks
of every hook and CLI command through the installed entry point, on a throwaway project; defaults to the fixture) · `python3 bench/simulate.py --sensitivity` ·
`python3 bench/replay_real.py` · `python3 bench/live_bench.py <dir> …` then `python3 bench/summarize.py <dir>/results.json`.
Raw live results: `bench/results_live.json`. ITE = input-token equivalents (cache read 0.1×, 1h cache write 2×, output 5×).

### Live A/B, real `claude -p` sessions, tokens read back from transcripts (final code)

| workload | baseline | ctxlc | change | correctness / retention (b / c) |
|---|---:|---:|---:|---:|
| Tool-heavy, 3 repetitions (failing build, 200k-char lint, failing tests, file read twice) | 711,904 ITE | 379,034 ITE | **−47 %** (−43 … −52) | 13/13 / 13/13 each |
| Model switch, Haiku work (69k ctx) → Sonnet quiz — measured cache writes | 191,037 | 97,119 | **−49 %** | 6/6 / 6/6 |
| Continuation, large (123k ctx), resume after cache expiry* | 249,064 | 68,536 | **−72 %** | 6/6 / 6/6 |
| Continuation, medium (71k) * | 145,824 | 72,709 | **−50 %** | 6/6 / 6/6 |
| Continuation, small (45k) * | 93,504 | 71,609 | −23 % | 6/6 / 6/6 |
| Continuation, small, cache still warm (measured) | 7,696 | 22,486 | **+192 %** | — |
| Auto-compaction at a forced 70k window (compaction verified: 72.9k → 15.4k) | 256,879 | 257,865 | +0.4 % | 6/6 / 6/6 |

\* Cold-cache cost is derived from the measured quiz request (its prefix re-written at 2× instead of read at
0.1×), which is what the audit shows happens after idle > TTL. With a warm cache, resuming is cheaper than
restoring, which is why both guards fire only when the cache is cold (or about to be discarded) *and* the
context is over 60k.

### Continuing after a break: ctxlc vs. resume, `/compact`, and a handoff file (`bench/vs_baselines.py`)
One 8-turn Haiku work session (71k context) that plants 13 facts — including a casual preference, a decision
reversal, a rejected approach, a bug the user says they fixed, an error buried in a 4,000-line build log and an
exact column order — then each arm continues from the identical history and answers a 13-question quiz.
Claude Code's auto-memory is off in every arm (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), so no arm can
recall a fact from memory files instead of its own context. Means of 3 repetitions; cost = transition + quiz, from `total_cost_usd`; cold ITE derived as above.

| arm | score (3 reps) | $ / continuation | new context | cold-cache ITE |
|---|---|---:|---:|---:|
| **ctxlc digest only** (no model work at the transition) | **13, 13, 13** | **0.020** | 29.3k | **63,491** |
| ctxlc + model-written handoff | 13, 13, 13 | 0.249 | 29.2k | 213,033 |
| resume the full conversation | 13, 13, 13 | 0.225 | 71.3k | 145,526 |
| `/compact` then continue | 13, 13, 13 | 0.507 | 31.0k | 214,358 |
| model writes HANDOFF.md, fresh session reads it | 12, 13, 13 | 0.264 | 27.8k | 227,674 |

Before the fixes in this release ctxlc alone scored 11, 12, 11 (`bench/results_vs_baselines_before_fixes.json`):
the build error was lost in Claude Code's 10k-char head of the log, piped test failures were never recorded, and
`amount_cents` lost its underscore. Caveats: one synthetic project, Haiku only; the cost table is for a warm
cache, where resuming is close to ctxlc in *tokens* but not in dollars here because the resume arm re-reads 71k.
An earlier run with auto-memory on (`bench/results_vs_baselines_automemory_on.json`) ranked the arms the same, with
each arm ~3k tokens larger and ctxlc again 13, 13, 13.

### Trace replay over this machine's 32 real sessions (simulation, not measurement)
Compaction at 160k + state restore on cold cache over 60k: **−56.3 %** ITE vs. the same replay with no policy
(−44.8 % … −58.4 % across re-read assumptions of 40k … 0 tokens). Windows ≤ 80k cost more than they save.
Offline replay of L1 over the real Bash/Read outputs: only −1.1 % — this user's real outputs are mostly small;
the large ones are screenshots/images, which ctxlc deliberately leaves alone. The big wins on real history come
from the triggers, not from output trimming.

### Overhead
Hook latency ≈ 28–30 ms per call (incl. 200k-char outputs). Ingestion ≤ 0.05 s for an 8 MB transcript.
Digest ≈ 0.5–2.5k tokens. No model calls are made by ctxlc itself.

### Large projects (`python3 bench/scale.py 3000 400`)
A synthetic multi-day session: 3,000 turns, a 30 MB transcript, 400 requirements across 400 modules, and a last
prompt about a module whose requirement was stated in the first 1 % of the session.

| measure | result |
|---|---|
| first full ingest of the 30 MB transcript · later `Stop` hooks | 1.6 s · 0.002 s |
| `UserPromptSubmit` · `PostToolUse` on a 200k-char output | 0.0002 s · 0.004 s |
| `state.json` | 116 KB |
| injected context after compaction | 9,384 chars (2.6k tokens), 71 requirements |
| the old requirement for the module being worked on | kept, in the digest and in the PreCompact list |

What keeps this from degrading work on big projects:
* **No compaction cap by default.** Earlier versions set auto-compaction to 160k, which wasted most of a
  1M-context window; `ctx install` now removes that old setting, and `--window N` is opt-in.
* **Relevance before recency.** When requirements, decisions, config and notes don't all fit, they are ranked by
  overlap (IDF-weighted) with the current task, the last prompts and the last status, so an old rule for the
  module in hand beats a recent unrelated one. The PreCompact list (40 items) is chosen the same way. Before this,
  the same run kept 1 requirement and lost the relevant one.
* **Templated rules don't replace each other.** "for billing, responses must …" and "for auth, responses must …"
  share most words; requirements, constraints, config and notes with different subjects stay active instead of
  being auto-superseded.
* **No silent truncation.** Claude Code 2.1.278 replaces hook context over 10,000 chars with a 2 KB preview and a
  file path (checked live: a 15,000-char injection lost its end marker, a 9,714-char one arrived whole). The whole
  injection is capped at `injection_max_chars` (9,500), even if `digest_budget_tokens` is raised.
* Up to 500 auto-detected user rules are kept active (was 25, which archived the oldest ones).

What other people run into, and how it compares (sources: GitHub issues, blogs, Reddit, papers; October 2026):
* *Rules and corrections given mid-session are lost or paraphrased after one or two compactions*
  ([#25999](https://claudeissues.com/issue/25999-persistent-state-across-context-compaction),
  [paterson](https://ianlpaterson.com/blog/stop-claude-code-from-lobotomizing-itself-mid-task/)). ctxlc
  re-injects them verbatim after every compaction; the common advice of turning auto-compact off is not needed.
* *Large SessionStart context silently cut to 2 KB*
  ([#70460](https://claudeissues.com/issue/70460-bug-sessionstart-hook-output-silently-truncated-at-10kb-model-never-sees-the-mis)):
  handled by the cap above.
* *Plugin hooks stop after compaction* ([#25655](https://claudeissues.com/issue/25655-all-plugin-hooks-stop-firing-after-context-compaction),
  2.1.39): not seen on 2.1.278, where each of two manual `/compact` runs with the plugin fired PreCompact and then
  the re-injection. *PreCompact not firing on auto-compaction* (2.1.105–2.1.114): not seen on 2.1.278. In a forced
  auto-compaction run (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=50000`, five ~82 KB reads), each of the 3 auto-compactions
  with the plugin fired PreCompact, PostCompact and the re-injection, and a rule stated at the start was still
  followed at the end. One extra PreCompact with no compaction after it also appears in the same run without
  ctxlc installed, so it is Claude Code's behaviour. One of six earlier manual `/compact` runs lost the rule;
  the cause was not found.
* *Compaction fails with "prompt is too long" when the window is exhausted* (several issues): outside ctxlc's
  control; Bash/Read condensing slows how fast the window fills.
* Other tools take the same shape: claude-mem and MemoryForge re-inject at `SessionStart(compact)` (claude-mem
  summarises with a model; ctxlc uses none); Cline's memory bank is hand-written markdown; Aider fits a ranked
  repo map into a fixed token budget, the same idea as the relevance-ranked digest.

## Storage, privacy, retention
`.claude/context/` is git-ignored and owner-only (mode 0700): archived outputs and history contain whatever
commands printed, including secrets. At each session startup, archived outputs and dedup epochs older than
14 days are deleted and a `history.jsonl` over 20 MB is cut to its newest half; state items are never deleted.
`ctx …` control prompts (panel buttons) are kept out of "recent messages" and requirement extraction, and
bypass the idle guard. All thresholds are in `ctxlc/config.py` and can be overridden per project in
`.claude/context/config.json`.

## Limitations
* Settings hooks cannot run `/compact` or `/clear`; the in-app mod's buttons can (Start fresh, Compact), but
  without the mod (CLI `-p`, VS Code panel, Cowork) the guards need one user action. Blocking the first prompt
  after a long idle means re-sending it once.
* Failing commands outside the recognised build/test/install families still reach the model as Claude Code's
  ~10k-char head (`PostToolUseFailure` cannot rewrite output).
* Deterministic extraction misses decisions phrased without cue words; the agent-note protocol covers them, but
  Haiku ignored it in every benchmark run, so plan on the deterministic layer carrying the load.
* Directive extraction is keyword-based: a pasted log containing "you must…" becomes a requirement. Pastes can't
  be excluded wholesale because users paste their specs too (this project's own brief arrived as a paste).
* Grep, WebFetch and MCP tool outputs are not condensed (their `tool_response` shapes are not verified yet).
* The in-app `clear_session` tool starts the next session as `startup`, not `clear`; state is restored either way.
* Locking uses `fcntl`: macOS/Linux only.
* The prefix of system prompt + tool definitions (16–68k per request here) is outside ctxlc's control.
* User-reported fixes are matched by shared words between the user's sentence and the failure's error lines
  ("unicode" ↔ `test_import_unicode_names`). A vague claim ("fixed it") closes nothing; a claim naming a word that
  also appears in an unrelated open failure would close that line too.
* Requirement detection is pattern-based. A descriptive predicate after ", and/but always|never" is skipped by verb
  form ("…, and never expires"), but other descriptive sentences can still be recorded, and a rule written as
  "…, and never <verb>s/<verb>ed …" would be missed. On this machine's 659 real prompts the classifier picks 172
  sentences; the preference rules added 18 of them, about 14 real rules.
* Live benchmark: n = 3 for the tool workload and the continuation comparison, n = 1 for the others, one
  synthetic project, Haiku/Sonnet only.
