"""Single entry point for all Claude Code hook events.

Contract: a hook must never break the user's session. Any internal error is
logged to .claude/context/errors.log and the hook exits 0 with no output, which
Claude Code treats as "no change".
"""

import hashlib
import json
import os
import sys
import time
import traceback

from . import config, ingest, sync, toolout
from .store import AGENT_PROTOCOL, COWORK_PROTOCOL, Store, _clip, by_relevance, focus_text, is_cowork, render_digest

# The uploaded plugin ships no executable (claude.ai rejects bin/), so there the package runs as a module.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = ((config.PYTHON_CMD + ' ' if config.WINDOWS and config.FROM_CHECKOUT else '') + '"' + config.CTX_PATH + '"'
       if os.path.isfile(config.CTX_PATH) else f'PYTHONPATH="{_PKG_PARENT}" {config.PYTHON_CMD} -m ctxlc')


def emit(obj):
    sys.stdout.write(json.dumps(obj))


def epoch_key(d):
    return f"{d.get('session_id', 'nosession')}__{d.get('agent_id') or 'main'}"


def last_turn_info(transcript_path, tail_bytes=400_000):
    """Timestamp, context size, model and cache TTL of the last main-thread response."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            lines = f.read().splitlines()
    except OSError:
        return None
    info = None
    for raw in reversed(lines):
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if o.get("type") != "assistant" or o.get("isSidechain"):
            continue
        m = o.get("message") or {}
        u = m.get("usage")
        if not u or m.get("model") == "<synthetic>":
            continue
        if info is None:
            info = {
                "ts": ingest.parse_ts(o.get("timestamp")),
                "ctx": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
                "model": m.get("model"),
                "ttl": 3600,
            }
        # A fully cached request writes nothing, so it says nothing about the TTL: use the
        # most recent response that did write to the cache.
        cc = u.get("cache_creation") or {}
        if cc.get("ephemeral_1h_input_tokens"):
            break
        if cc.get("ephemeral_5m_input_tokens"):
            info["ttl"] = 300
            break
    return info


# --- handlers ---------------------------------------------------------------------------
def on_session_start(store, cfg, d):
    source = d.get("source", "startup")
    with store.transaction() as st:
        n = ingest.ingest_all(store, st, d.get("transcript_path"))
        has_state = bool(st["items"] or st["prompts"] or st["files"])
        sid = d.get("session_id")
        cowork = is_cowork(d.get("cwd"))
        protocol = COWORK_PROTOCOL if cowork else AGENT_PROTOCOL.format(cli=CLI)
        if sync.needs_pull(cfg, st):
            protocol += "\n\n" + sync.pull_instructions(cfg, CLI)
        digest = render_digest(st, cfg["digest_budget_tokens"], cfg["recent_prompts_kept"], None if cowork else CLI,
                               session=sid, handoff_ttl_s=cfg["handoff_ttl_s"],
                               state_path=os.path.join(store.dir, "state.json"),
                               max_chars=cfg["injection_max_chars"] - len(protocol) - 2) if has_state else ""
        ho = st.get("handoff")
        if source != "resume" and ho and not ho.get("consumed_by") and sid:
            ho.update(consumed_by=sid, consumed_at=time.time())
    if source in ("startup", "clear", "compact"):
        store.reset_epoch(epoch_key(d))
    if source == "startup":
        removed, dropped = store.gc(cfg["artifact_max_age_days"], cfg["history_max_bytes"])
        if removed or dropped:
            store.metric("gc", files=removed, history_bytes=dropped)
    if source == "resume":
        # The resumed transcript already contains everything injected earlier.
        store.metric("session_start", source=source, session=d.get("session_id"), ingested=n)
        return
    ctx = (digest + "\n\n" if digest else "") + protocol
    store.metric("inject", source=source, session=d.get("session_id"), chars=len(ctx), ingested=n)
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}})


def on_post_tool_use(store, cfg, d):
    name = d.get("tool_name")
    resp = d.get("tool_response")
    shell_mcp = bool(toolout.SHELL_MCP.match(name or ""))
    if shell_mcp:
        text = toolout.mcp_text(resp)
        if text is None:
            return
        resp = {"stdout": text, "stderr": ""}
    elif name not in ("Bash", "Read") or not isinstance(resp, dict):
        return
    key = epoch_key(d)
    with store.locked():
        ep = store.load_epoch(key)
        if name == "Bash" or shell_mcp:
            new, m = toolout.compact_bash(store, cfg, ep, d.get("tool_input") or {}, resp,
                                    call_id=d.get("tool_use_id"))
            if new is not None and shell_mcp:
                new = toolout.mcp_replace(d.get("tool_response"), new["stdout"])
        else:
            new, m = toolout.dedup_read(cfg, ep, d.get("tool_input") or {}, resp,
                                    call_id=d.get("tool_use_id"))
        store.save_epoch(key, ep)
    if new is None:
        return
    store.metric("tool", session=d.get("session_id"), agent=d.get("agent_id"), tool_use_id=d.get("tool_use_id"), **m)
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}})


def on_pre_tool_use(store, cfg, d):
    name = d.get("tool_name") or ""
    if not (name == "Bash" or toolout.SHELL_MCP.match(name)) or not cfg["wrap_noisy_commands"]:
        return
    new = toolout.wrap_command(d.get("tool_input") or {})
    if new:
        # No permissionDecision: the normal permission flow still applies to the call.
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new}})


def on_stop(store, cfg, d):
    with store.transaction() as st:
        ingest.ingest_transcript(store, st, d.get("transcript_path", ""), d.get("session_id"))
        reason = sync.stop_push(st, cfg, d)
    if reason:
        store.metric("sync_push_request", session=d.get("session_id"), chars=len(reason))
        emit({"decision": "block", "reason": reason})


def on_ingest_only(store, cfg, d):
    with store.transaction() as st:
        ingest.ingest_transcript(store, st, d.get("transcript_path", ""), d.get("session_id"))


COMPACT_INSTRUCTIONS = """ctxlc keeps this project's durable state outside the conversation and re-injects it right after this \
compaction, so the summary does not need to carry it. The following are already preserved, with these IDs:
{items}
In the summary, refer to these by ID instead of restating them. Spend the space on what is NOT in that list: the exact \
step in progress, findings and approaches already ruled out (and why), files and lines being changed, uncommitted \
reasoning, and the next concrete action. Anything the user asked for that is missing from the list must still be \
preserved in full."""


def on_pre_compact(store, cfg, d):
    with store.transaction() as st:
        ingest.ingest_transcript(store, st, d.get("transcript_path", ""), d.get("session_id"))
        items = [it for it in st["items"] if it["status"] == "active"]
        failures = [f for f in st.get("failures", []) if not f.get("resolved") and f.get("id")]
        # Newest first, then the ones the current work mentions; list 40 of them in the order they were recorded.
        keep = {it["id"] for it in by_relevance(items[::-1], focus_text(st))[:40]}
    lines = [f"- {it['id']} ({it['kind']}): {_clip(it['text'], 90)}" for it in items if it["id"] in keep]
    lines += [f"- {f['id']} (open failure): `{_clip(f['cmd'], 60)}`" for f in failures[-10:]]
    if not lines:
        return
    store.metric("pre_compact", trigger=d.get("trigger"), session=d.get("session_id"), items=len(lines))
    # Exit 0 with plain stdout: Claude Code appends it to the compaction instructions.
    sys.stdout.write(COMPACT_INSTRUCTIONS.format(items="\n".join(lines)))


def on_post_compact(store, cfg, d):
    summary = d.get("compact_summary") or ""
    with store.transaction() as st:
        ingest.ingest_transcript(store, st, d.get("transcript_path", ""), d.get("session_id"))
        st["last_compact_summary"] = {"ts": time.time(), "session": d.get("session_id"), "text": summary}
    store.history({"ts": time.time(), "session": d.get("session_id"), "kind": "compact_summary", "text": summary})
    store.reset_epoch(epoch_key(d))


def on_pre_model_switch(store, cfg, d):
    on_ingest_only(store, cfg, d)
    ctx = d.get("context_tokens") or 0
    warm = d.get("prompt_cache_warm")
    store.metric("model_switch_request", session=d.get("session_id"), from_model=d.get("from_model"), to_model=d.get("to_model"),
                 context_tokens=ctx, warm=warm, est_usd=d.get("estimated_cache_write_usd"))
    if cfg["switch_guard"] == "off" or not warm or ctx < cfg["switch_guard_min_tokens"]:
        return
    usd = d.get("estimated_cache_write_usd")
    reason = (
        f"ctxlc: switching {d.get('from_model')} → {d.get('to_model')} discards the warm prompt cache; the next request "
        f"re-caches the whole ~{ctx // 1000}k-token context" + (f" (≈${usd:.2f})" if isinstance(usd, (int, float)) else "") + ". "
        "Cheaper: cancel, run /compact (reuses the warm cache) or /clear (project state is restored automatically), then switch."
    )
    emit({"hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "ask", "permissionDecisionReason": reason}})


def on_post_model_switch(store, cfg, d):
    store.metric("model_switch", session=d.get("session_id"), from_model=d.get("from_model"), to_model=d.get("to_model"),
                 context_tokens=d.get("context_tokens"), source=d.get("source"))


def on_user_prompt_submit(store, cfg, d):
    if ingest.CONTROL.match((d.get("prompt") or "").strip()):
        return
    info = last_turn_info(d.get("transcript_path", ""))
    if not info or not info["ts"]:
        return
    idle = time.time() - info["ts"]
    if idle > info["ttl"]:
        if cfg["idle_guard"] != "off" and info["ctx"] >= cfg["idle_guard_min_tokens"]:
            idle_guard(store, cfg, d, info, idle)
    else:
        context_nudge(store, cfg, d, info)


def _guard_log(store, update=None):
    """Outcomes of recent idle warnings in this project: "warned" (held) or "sent" (sent again anyway)."""
    path = os.path.join(store.dir, "guard.json")
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = []
    if update:
        log = update(log)[-10:]
        store.ensure()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f)
    return log


def idle_guard(store, cfg, d, info, idle):
    marker = store.epoch_path("idleguard__" + d.get("session_id", ""))
    try:
        shown = os.path.getmtime(marker)
    except OSError:
        shown = None
    if shown and time.time() - shown < cfg["guard_override_window_s"]:
        # Keep the marker: a second copy of this hook (e.g. a stale plugin) runs for the same submit and must pass too.
        store.metric("idle_guard_override", session=d.get("session_id"), idle_s=int(idle), ctx=info["ctx"])
        _guard_log(store, lambda log: log[:-1] + ["sent"] if log and log[-1] == "warned" else log + ["sent"])
        return
    hours = idle / 3600
    cost = (f"this conversation was idle {hours:.1f}h, so its prompt cache ({info['ttl'] // 60} min TTL) has expired. "
            f"Sending now re-caches the full ~{info['ctx'] // 1000}k-token context before any work starts.")
    fresh = ("start a new session in this folder and send your message there. Project state (requirements, "
             "decisions, current task, files, recent messages) is restored automatically, and this conversation stays "
             "in your session list to read (or `ctx history`). /clear here works the same but hides this chat.")
    # "auto": after the user sent anyway on 2 of the last 3 warnings, stop holding the prompt and just say it.
    if cfg["idle_guard"] == "auto" and _guard_log(store)[-3:].count("sent") >= 2:
        store.metric("idle_guard_notice", session=d.get("session_id"), idle_s=int(idle), ctx=info["ctx"])
        emit({"systemMessage": f"ctxlc: sent. Note: {cost} Next time it is cheaper to {fresh} (You sent anyway on "
                               "recent warnings, so ctxlc no longer holds the message; set \"idle_guard\": \"ask\" in "
                               ".claude/context/config.json to be asked again.)"})
        return
    store.ensure()
    open(marker, "w", encoding="utf-8").close()
    store.metric("idle_guard_block", session=d.get("session_id"), idle_s=int(idle), ctx=info["ctx"])
    _guard_log(store, lambda log: log + ["warned"])
    emit({"decision": "block", "reason": f"ctxlc: {cost} Cheaper: {fresh} To keep the full history anyway, just send "
                                         "the same message again."})


def context_nudge(store, cfg, d, info):
    """Once per size band (150k, 250k, 400k by default), tell the user what a large conversation costs per message.

    Non-blocking. Shown only while the cache is warm, which is exactly when /compact is cheapest."""
    passed = [b for b in sorted(cfg["context_nudge_tokens"] or []) if info["ctx"] >= b]
    path = store.epoch_path("nudge__" + d.get("session_id", ""))
    try:
        with open(path, encoding="utf-8") as f:
            shown = json.load(f).get("band", 0)
    except (OSError, ValueError):
        shown = 0
    if passed and passed[-1] <= shown:
        return
    if shown and (not passed or info["ctx"] < shown):  # compacted below the band shown: start over
        os.remove(path)
        shown = 0
    if not passed:
        return
    store.ensure()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"band": passed[-1]}, f)
    k = info["ctx"] // 1000
    store.metric("context_nudge", session=d.get("session_id"), ctx=info["ctx"], band=passed[-1])
    emit({"systemMessage": f"ctxlc: this conversation is ~{k}k tokens, and every message re-reads all of it (about "
                           f"{k / 10:.0f}k tokens' worth at the cache-read rate, before any new work). The cheapest "
                           "moment to shrink it is now, while the cache is warm: /compact keeps this thread, or a new "
                           "session in this folder restores the project state for ~2k tokens."})


HANDLERS = {
    "SessionStart": on_session_start,
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": on_post_tool_use,
    "Stop": on_stop,
    "PreCompact": on_pre_compact,
    "SessionEnd": on_ingest_only,
    "PostCompact": on_post_compact,
    "PreModelSwitch": on_pre_model_switch,
    "PostModelSwitch": on_post_model_switch,
    "UserPromptSubmit": on_user_prompt_submit,
}


def installed_hooks_present(cwd):
    """True when settings (user or this project's) register ctxlc's own `ctx … hook` command."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    paths = [os.path.join(home, "settings.json")]
    if cwd:
        paths += [os.path.join(cwd, ".claude", n) for n in ("settings.json", "settings.local.json")]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                groups = json.load(f).get("hooks", {}).values()
        except (OSError, ValueError, AttributeError):
            continue
        for g in (g for gs in groups for g in gs if isinstance(g, dict)):
            if any("ctx" in h.get("command", "") and h.get("command", "").endswith(" hook")
                   and "CTXLC_PLUGIN" not in h.get("command", "") for h in g.get("hooks", [])):
                return True
    return False


def first_copy(store, raw, window_s=5):
    """False when another copy of ctxlc's hooks already took this exact event.

    Two registrations (installed hooks plus a stale plugin copy, user plus project settings, ...) both receive the
    same input for the same event. Running twice double-injects state, makes the second PostToolUse copy call the
    output a repeat of itself, and made the idle warning block every resend. Whichever copy claims the event first
    handles it; the same input with a longer transcript, or more than window_s later, is a new event."""
    try:  # a later event with the same input comes after the conversation grew; a duplicate copy sees the same size
        raw += f"\0{os.path.getsize(json.loads(raw).get('transcript_path') or '')}"
    except (OSError, ValueError, AttributeError):
        pass
    os.makedirs(store.runs, exist_ok=True)
    path = os.path.join(store.runs, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32])
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            pass
        try:
            if time.time() - os.path.getmtime(path) < window_s:
                return False
            stale = f"{path}.{os.getpid()}"
            os.rename(path, stale)  # only one copy wins the rename of an old marker
            os.remove(stale)
        except OSError:
            return False
    return False


def main(stdin=None):
    raw = (stdin or sys.stdin).read()
    store = None
    try:
        d = json.loads(raw)
        handler = HANDLERS.get(d.get("hook_event_name"))
        if not handler:
            return 0
        if os.environ.get("CTXLC_PLUGIN") and installed_hooks_present(d.get("cwd")):
            return 0  # the plugin copy beside a `ctx install`: those hooks already do this
        store = Store(ingest_project(d))
        if not first_copy(store, raw):
            return 0
        handler(store, config.load(store.dir), d)
    except Exception:
        try:
            s = store or Store()
            s.ensure()
            with open(os.path.join(s.dir, "errors.log"), "a", encoding="utf-8") as f:
                f.write(f"--- {time.ctime()}\n{traceback.format_exc()}\n")
        except Exception:
            pass
    return 0


def ingest_project(d):
    from .store import project_dir
    return project_dir(d.get("cwd"))
