"""`ctx` command line: state notes, retrieval, reporting, install."""

import argparse
import json
import os
import re
import shutil
import sys
import time

from . import config, history, hooks, ingest, panel, plugin, report, retrieve, sync, tokens
from .store import KIND_PREFIX, Store, add_item, fmt_ts, render_digest, set_status

CTX_PATH = config.CTX_PATH
# A checkout's bin/ctx is run with python3 (it may lack the executable bit); an installed console script carries
# its own interpreter in its shebang, and running it with the system python3 could not import ctxlc.
HOOK_CMD = (config.PYTHON_CMD + ' "' if config.FROM_CHECKOUT else '"') + CTX_PATH + '" hook'
SKILL_DIR = os.path.expanduser("~/.claude/skills/ctx")
# The in-app status line / band / pane. Claude Code auto-loads a plugin folder under ~/.claude/skills/.
MOD_DIR = os.path.expanduser("~/.claude/skills/ctxlc-bar")
MOD_FILES = (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.tsx", "types/index.d.ts")
HOOK_EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "Bash|mcp__.*__(device_bash|bash)",
    "PostToolUse": "Bash|Read|mcp__.*__(device_bash|bash)",
    "Stop": None,
    "PreCompact": None,
    "PostCompact": None,
    "PreModelSwitch": None,
    "PostModelSwitch": None,
    "SessionEnd": None,
}
# Earlier ctxlc builds always set the auto-compact window to this. That capped 1M-context sessions at ~147k tokens,
# which costs large projects context they need, so the window is now opt-in (--window) and the old value is removed.
LEGACY_AUTO_COMPACT_WINDOW = "160000"


def cmd_note(a, store):
    with store.transaction() as st:
        item, replaced = add_item(st, a.kind, a.text, source="agent", supersedes=a.supersedes)
    print(item["id"] + (f" (supersedes {', '.join(replaced)})" if replaced else ""))


def cmd_task(a, store):
    a.kind, a.supersedes = "task", None
    cmd_note(a, store)


def cmd_handoff(a, store):
    text = a.text if a.text != "-" else sys.stdin.read()
    with store.transaction() as st:
        ingest.ingest_all(store, st)
        last = max(st["sessions"].values(), key=lambda x: x.get("last_ts") or 0, default={})
        st["handoff"] = {"ts": time.time(), "model": last.get("model"), "text": text.strip()}
    print(f"handoff saved ({len(text.strip()):,} chars); it is injected at the top of the next session start / clear / compact")


def status(store, session=None):
    """Everything the /ctx panel shows, from local state only (no model calls).

    session: report that session's context and cache (the in-app mod passes its own id; a session with no turn yet
    reports none). Default: the project's most recent session."""
    cfg = config.load(store.dir)
    with store.transaction() as st:
        ingest.ingest_all(store, st)
        digest = render_digest(st, cfg["digest_budget_tokens"], cfg["recent_prompts_kept"], handoff_ttl_s=cfg["handoff_ttl_s"])
    rep = report.build(store)
    if session:
        sess = (session, st["sessions"].get(session, {}))
    else:
        sess = max(st["sessions"].items(), key=lambda kv: kv[1].get("last_ts") or 0, default=(None, {}))
    sid, s = sess
    idle = time.time() - s["last_ts"] if s.get("last_ts") else None
    ttl = s.get("ttl", 3600)
    active = [i for i in st["items"] if i["status"] == "active"]
    count = lambda *kinds: sum(1 for i in active if i["kind"] in kinds)
    bugs = [{"id": i["id"], "text": i["text"][:200]} for i in active if i["kind"] in ("bug", "issue")]
    bugs += [{"id": f.get("id"), "text": f"{f['cmd'][:80]} → {f['err'][:120]}"} for f in st.get("failures", []) if not f.get("resolved")][-5:]
    task = [i["text"] for i in active if i["kind"] == "task"][:1]
    tool = rep["tool"]
    return {
        "project": store.project,
        "session": sid, "model": s.get("model"), "context_tokens": s.get("last_ctx"),
        "idle_s": round(idle) if idle is not None else None, "cache_ttl_s": ttl,
        "cache_warm": idle is not None and idle < ttl,
        "auto_compact_window": int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 0) or None,
        "task": task[0] if task else None,
        "handoff": st.get("handoff"),
        "counts": {"requirements": count("requirement", "constraint"), "decisions": count("decision"),
                   "open_bugs": len(bugs), "notes": count("note", "config"), "prompts_logged": len(st.get("prompts", []))},
        "open_bugs": bugs,
        "digest_tokens": tokens.estimate(digest),
        "savings": {"outputs_condensed": int(sum(t["count"] for t in tool.values())),
                    "tokens_removed": int(sum(t["raw_tokens"] - t["new_tokens"] for t in tool.values())),
                    "downstream_ite_saved": int(rep["downstream_ite_saved"])},
        "guards": rep["guards"],
    }


def cmd_status(a, store):
    s = status(store, a.session)
    if a.json:
        print(json.dumps(s, indent=1))
        return
    ctx = f"{s['context_tokens']:,}" if s["context_tokens"] else "?"
    cache = "warm" if s["cache_warm"] else "cold"
    print(f"{s['project']}\nsession {s['session']} · {s['model']} · context {ctx} tokens · cache {cache} (idle {s['idle_s']}s, ttl {s['cache_ttl_s']}s)")
    print(f"items: {s['counts']} · digest ~{s['digest_tokens']:,} tokens")
    print(f"saved so far: {s['savings']['outputs_condensed']} outputs condensed, ~{s['savings']['tokens_removed']:,} tokens removed, ~{s['savings']['downstream_ite_saved']:,} ITE downstream")
    for b in s["open_bugs"]:
        print(f"  open [{b['id']}] {b['text']}")


def cmd_panel(a, store):
    usage = sys.stdin.read() if a.usage == "-" else (a.usage or "")
    print(panel.main_from_cli(status(store), usage))


def cmd_resolve(a, store):
    with store.transaction() as st:
        it = set_status(st, a.id, a.status)
    print(f"{a.id} → {a.status}" if it else f"no such item {a.id}")


def cmd_state(a, store):
    with store.transaction() as st:
        ingest.ingest_all(store, st)
        if a.json:
            print(json.dumps(st, indent=1))
            return
        if not a.all:
            print(render_digest(st, budget_tokens=a.budget or 10**6))
            return
        for it in st["items"]:
            print(f"[{it['id']}] {it['status'].upper():10} {it['kind']:11} {fmt_ts(it['updated'])}  {it['text']}"
                  + (f"  → {it['superseded_by']}" if it.get("superseded_by") else ""))


def cmd_digest(a, store):
    cfg = config.load(store.dir)
    with store.transaction() as st:
        ingest.ingest_all(store, st)
        print(render_digest(st, a.budget or cfg["digest_budget_tokens"], cfg["recent_prompts_kept"], handoff_ttl_s=cfg["handoff_ttl_s"]))


def cmd_search(a, store):
    st = store.load()
    hits = retrieve.search(store, st, " ".join(a.query), k=a.k)
    if not hits:
        print("no matches")
    for h in hits:
        print(f"[{h['ref']}] {h['label']} {fmt_ts(h['ts'])} (score {h['score']})\n    {h['snippet']}")


def cmd_show(a, store):
    print(retrieve.show(store, store.load(), a.ref, grep=a.grep, lines=a.lines))


def cmd_report(a, store):
    rep = report.build(store)
    print(json.dumps(rep, indent=1, default=str) if a.json else report.render(rep))


def cmd_sync_import(a, store):
    if a.off:
        with store.transaction() as st:
            sync.mark_off(st)
        print("state sync is off for this task")
        return
    if a.folder is not None and (not a.folder.strip() or "'" in a.folder):
        sys.exit("--folder must be a non-empty path without single quotes")
    data = None if a.none else json.loads(sys.stdin.read())
    cfg = config.load(store.dir)
    with store.transaction() as st:
        ingest.ingest_all(store, st)
        if data:
            sync.merge(st, data)
        sync.mark_ready(st, (a.folder.rstrip("/") or "/") if a.folder else None)
        out = render_digest(st, cfg["digest_budget_tokens"], cfg["recent_prompts_kept"], handoff_ttl_s=cfg["handoff_ttl_s"])
    print(out if data else "no saved state; this task's state will be saved to the folder as it changes")


def cmd_ingest(a, store):
    with store.transaction() as st:
        n = ingest.ingest_all(store, st)
    print(f"ingested {n} transcript lines")


def settings_path(a):
    if a.global_:
        return os.path.expanduser("~/.claude/settings.json")
    return os.path.join(os.path.abspath(a.project or os.getcwd()), ".claude", "settings.local.json")


# ".../bin/ctx" hook (checkout, pipx on macOS/Linux) or ".../Scripts/ctx.exe" hook (Windows).
OUR_HOOK = re.compile(r'[/\\]ctx(?:\.exe)?"? hook$')


def _is_ours(g):
    return any(OUR_HOOK.search(h.get("command", "")) for h in g.get("hooks", []))


def _read_settings(path):
    """Existing settings, {} if absent. A file that exists but is not valid JSON is an error:
    overwriting it would silently drop the user's other settings."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError as e:
        sys.exit(f"ctx: {path} is not valid JSON ({e}); fix it first, nothing was changed")


def _write_settings(path, s):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".ctxlc.bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # LF on Windows too: don't churn the user's file
        json.dump(s, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def install_skill():
    """Write ~/.claude/skills/ctx/SKILL.md with this install's absolute ctx path."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_template.md"), encoding="utf-8") as f:
        text = f.read().replace("__CTX__", CTX_PATH)
    path = os.path.join(SKILL_DIR, "SKILL.md")
    os.makedirs(SKILL_DIR, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path, False
        shutil.copy2(path, path + ".ctxlc.bak")
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, True


def install_mod(dest=None):
    """Copy ctxlc/mod to ~/.claude/skills/ctxlc-bar with the argv that runs this install's ctx."""
    dest = dest or MOD_DIR
    argv = [config.PYTHON, CTX_PATH] if config.FROM_CHECKOUT else [CTX_PATH]
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod")
    changed = False
    for rel in MOD_FILES:
        with open(os.path.join(src, rel), encoding="utf-8") as f:
            text = f.read().replace("__CTX_ARGV__", json.dumps(argv))
        path = os.path.join(dest, rel)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    continue
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        changed = True
    return dest, changed


def cmd_install(a, store):
    path = settings_path(a)
    s = _read_settings(path)
    before = json.loads(json.dumps(s))
    hooks_cfg = s.setdefault("hooks", {})
    for ev, matcher in HOOK_EVENTS.items():
        groups = [g for g in hooks_cfg.get(ev, []) if not _is_ours(g)]
        g = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 30}]}
        if matcher:
            g["matcher"] = matcher
        groups.append(g)
        hooks_cfg[ev] = groups
    env = s.setdefault("env", {})
    if a.window:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(a.window)
    elif env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == LEGACY_AUTO_COMPACT_WINDOW:
        del env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
        print(f"removed CLAUDE_CODE_AUTO_COMPACT_WINDOW={LEGACY_AUTO_COMPACT_WINDOW} set by an earlier ctxlc "
              "(auto-compaction now uses the model's full window; pass --window N to cap it)")
    if not env:
        del s["env"]
    if s == before:
        # Rewriting would replace the backup of the pre-install settings with this identical copy.
        print(f"ctxlc hooks already installed in {path}; nothing changed")
    else:
        _write_settings(path, s)
        print(f"installed ctxlc hooks into {path} (previous version: {path}.ctxlc.bak)" if os.path.exists(path + ".ctxlc.bak") else f"installed ctxlc hooks into {path}")
    if not a.no_skill:
        sp, changed = install_skill()
        print(f"{'installed' if changed else 'up to date:'} /ctx skill {sp}")
        mp, changed = install_mod()
        print(f"{'installed' if changed else 'up to date:'} in-app status line and /ctxlc pane {mp}")
    print("start a new Claude Code session (or /clear) for the hooks to load")


def cmd_uninstall(a, store):
    path = settings_path(a)
    s = _read_settings(path)
    hooks_cfg = s.get("hooks", {})
    n = 0
    for ev in list(hooks_cfg):
        kept = [g for g in hooks_cfg[ev] if not _is_ours(g)]
        n += len(hooks_cfg[ev]) - len(kept)
        if kept:
            hooks_cfg[ev] = kept
        else:
            del hooks_cfg[ev]
    if "hooks" in s and not s["hooks"]:
        del s["hooks"]
    if s.get("env", {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") in (LEGACY_AUTO_COMPACT_WINDOW, str(a.window or "")):
        del s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
        n += 1
        if not s["env"]:
            del s["env"]
    if n:
        _write_settings(path, s)
        print(f"removed ctxlc hooks from {path} (previous version: {path}.ctxlc.bak)")
    else:
        print(f"no ctxlc hooks in {path}; nothing changed")
    if a.global_ and not a.no_skill and os.path.isdir(SKILL_DIR):
        shutil.rmtree(SKILL_DIR)
        print(f"removed /ctx skill {SKILL_DIR}")
    if a.global_ and not a.no_skill and os.path.isdir(MOD_DIR):
        shutil.rmtree(MOD_DIR)
        print(f"removed in-app status line and /ctxlc pane {MOD_DIR}")
    print("project state in .claude/context/ is kept; delete it yourself if you no longer need it")


def cmd_history(a, store):
    if not a.ref:
        convs = history.conversations(store.project)
        if a.json:
            cur = status(store).get("session")
            print(json.dumps([{"n": i, "id": c["id"], "start": c["start"], "end": c["end"], "prompts": c["prompts"],
                               "first": " ".join((c["first"] or "").split())[:200], "current": c["id"] == cur}
                              for i, c in enumerate(convs[:a.n], 1)]))
            return
        if not convs:
            print("no saved conversations for this project")
        for i, c in enumerate(convs[:a.n], 1):
            # The app's title is the same for every conversation in one app session; the first prompt is not.
            first = " ".join((c["first"] or "").split())
            print(f"{i:>2}. {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['end']))}  {c['prompts']:>3} prompts  {c['id'][:8]}  {first[:70]}")
        if len(convs) > a.n:
            print(f"… {len(convs) - a.n} older (--n {len(convs)})")
        return
    c = history.find(store.project, a.ref)
    if not c:
        sys.exit(f"no single conversation matches {a.ref!r}; run `ctx history` for the list")
    if a.md:
        print(history.markdown(c))
        return
    out = os.path.join(store.dir, "history", c["id"] + ".html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(history.render(c))
    print(out)
    if a.open:
        if config.WINDOWS:
            os.startfile(out)
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(opener):
            os.spawnlp(os.P_WAIT, opener, opener, out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Claude Code exchanges UTF-8 with hooks; Windows (and C-locale Linux) would otherwise use a legacy code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")
    if argv[:1] == ["hook"]:
        return hooks.main()
    p = argparse.ArgumentParser(prog="ctx", description="Claude Code context lifecycle manager")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("note", help="record a requirement/constraint/decision/bug/issue/config/note")
    n.add_argument("kind", choices=sorted(KIND_PREFIX))
    n.add_argument("text")
    n.add_argument("--supersedes", nargs="*")
    n.set_defaults(fn=cmd_note)
    t = sub.add_parser("task", help="set the current task")
    t.add_argument("text")
    t.set_defaults(fn=cmd_task)
    r = sub.add_parser("resolve", help="mark an item resolved/done/superseded")
    r.add_argument("id")
    r.add_argument("--status", default="resolved", choices=["resolved", "done", "superseded", "active"])
    r.set_defaults(fn=cmd_resolve)
    h = sub.add_parser("handoff", help="save a handoff note (what a fresh context needs to continue); '-' reads stdin")
    h.add_argument("text")
    h.set_defaults(fn=cmd_handoff)
    stt = sub.add_parser("status", help="context/cache/state summary (what the /ctx panel shows)")
    stt.add_argument("--json", action="store_true")
    stt.add_argument("--session", help="report this session's context and cache (default: the most recent session)")
    stt.set_defaults(fn=cmd_status)
    pn = sub.add_parser("panel", help="HTML for the /ctx control panel")
    pn.add_argument("--usage", help="get_usage JSON from the desktop app ('-' reads stdin)")
    pn.set_defaults(fn=cmd_panel)
    s = sub.add_parser("state", help="show durable state")
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--budget", type=int)
    s.set_defaults(fn=cmd_state)
    d = sub.add_parser("digest", help="print the digest injected at session start")
    d.add_argument("--budget", type=int)
    d.set_defaults(fn=cmd_digest)
    hi = sub.add_parser("history", help="list past conversations (kept after /clear); `history N --open` reads one")
    hi.add_argument("ref", nargs="?", help="list number (1 = newest) or session id prefix")
    hi.add_argument("--open", action="store_true", help="open the rendered page in the browser")
    hi.add_argument("--n", type=int, default=15)
    hi.add_argument("--json", action="store_true", help="the list as JSON")
    hi.add_argument("--md", action="store_true", help="print the conversation as Markdown")
    hi.set_defaults(fn=cmd_history)
    q = sub.add_parser("search", help="search older history, items and archived outputs")
    q.add_argument("query", nargs="+")
    q.add_argument("-k", type=int, default=6)
    q.set_defaults(fn=cmd_search)
    sh = sub.add_parser("show", help="show a history entry (hN), item (D3) or archived output (a:<hash>)")
    sh.add_argument("ref")
    sh.add_argument("--grep")
    sh.add_argument("--lines", help="a:b (1-based)")
    sh.set_defaults(fn=cmd_show)
    pl = sub.add_parser("plugin", help="build the Cowork plugin zip (upload it in Claude → Customize → Plugins)")
    pl.add_argument("--out", default="ctxlc-plugin.zip")
    sy = pl.add_mutually_exclusive_group()
    sy.add_argument("--sync-folder", help="keep state between Cowork cloud tasks in this one folder on your computer, "
                                          "instead of the default: each task's own project folder")
    sy.add_argument("--no-sync", action="store_true", help="do not keep state between Cowork cloud tasks (sync costs a "
                                                           "copy through the model at task start and on durable changes)")
    pl.set_defaults(fn=lambda a, store: print("wrote " + plugin.build(a.out, "off" if a.no_sync else a.sync_folder)))
    si = sub.add_parser("sync-import", help="Cowork sync: merge state JSON from stdin (used by the injected instructions)")
    si.add_argument("--none", action="store_true", help="no saved state exists; allow saving this task's state")
    si.add_argument("--folder", help="the folder on the user's computer this task syncs with (default sync mode)")
    si.add_argument("--off", action="store_true", help="this task has no folder: do not sync it")
    si.set_defaults(fn=cmd_sync_import)
    rp = sub.add_parser("report", help="token accounting")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_report)
    sub.add_parser("ingest").set_defaults(fn=cmd_ingest)
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        i = sub.add_parser(name)
        i.add_argument("--project", help="project dir (default: cwd); writes .claude/settings.local.json")
        i.add_argument("--global", dest="global_", action="store_true", help="write ~/.claude/settings.json")
        i.add_argument("--window", type=int, metavar="TOKENS",
                       help="set CLAUDE_CODE_AUTO_COMPACT_WINDOW so auto-compaction triggers near TOKENS (default: not set; "
                            "the model's full window is used)")
        i.add_argument("--no-window", action="store_true", help=argparse.SUPPRESS)  # older flag; not setting the window is now the default
        i.add_argument("--no-skill", action="store_true", help="don't install/remove the /ctx skill and the ctxlc-bar mod in ~/.claude/skills")
        i.set_defaults(fn=fn)
    a = p.parse_args(argv)
    store = Store(os.path.abspath(a.project)) if getattr(a, "project", None) else Store()
    a.fn(a, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
