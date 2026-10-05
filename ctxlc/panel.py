"""HTML for the /ctx control panel (rendered inline in the Claude desktop app).

Buttons call sendPrompt("ctx <action>"), which reaches the model as a user
message; the ctx skill carries the action out. No leading slash: the desktop
app drops widget prompts that start with "/" (observed). Data comes from `ctx status`
plus, when available, the app's live usage numbers (get_usage).
"""

import html
import json


def recommendation(s, u):
    ctx = (u.get("context") or {})
    pct = ctx.get("percentUsed")
    at = ctx.get("autoCompactsAtPercent")
    tokens = ctx.get("tokensUsed") or s.get("context_tokens") or 0
    if not s["cache_warm"] and tokens >= 60000:
        return "warn", f"Cache is cold: your next message re-caches ~{tokens // 1000}k tokens. Smart reset continues from ~{s['digest_tokens'] // 1000 + 1}k."
    if pct is not None and at and pct >= at - 15:
        return "warn", f"Context is {pct}% full; auto-compaction starts at {at}%. A smart reset now keeps exact state instead of a lossy summary."
    if tokens >= 100000:
        return "info", f"Context is ~{tokens // 1000}k tokens. Every request re-reads it; a smart reset at a natural break is cheaper."
    return "ok", "Nothing to do. Hooks are condensing output and saving state automatically."


def render(s, u=None):
    u = u or {}
    e = lambda x: html.escape(str(x), quote=True)
    ctx = u.get("context") or {}
    tokens = ctx.get("tokensUsed") or s.get("context_tokens")
    window = ctx.get("contextWindow") or s.get("auto_compact_window")
    pct = ctx.get("percentUsed")
    idle = s.get("idle_s")
    cache = ("warm" if s["cache_warm"] else "cold") + (f", idle {idle // 60}m of {s['cache_ttl_s'] // 60}m" if idle is not None else "")
    plan = [(w["label"], w["percentUsed"], w.get("resetsIn", "")) for w in (u.get("plan") or {}).get("windows", [])]
    level, rec = recommendation(s, u)
    tone = {"warn": "warning", "info": "accent", "ok": "success"}[level]
    c = s["counts"]

    def metric(label, value, sub=""):
        return (f'<div style="background:var(--surface-1);border-radius:var(--radius);padding:.75rem 1rem">'
                f'<div style="font-size:13px;color:var(--text-secondary)">{e(label)}</div>'
                f'<div style="font-size:22px;font-weight:500">{e(value)}</div>'
                f'<div style="font-size:12px;color:var(--text-muted)">{e(sub)}</div></div>')

    def btn(label, action, icon):
        return (f'<button data-a="{e(action)}"><i class="ti ti-{icon}" aria-hidden="true" style="font-size:16px;vertical-align:-2px;margin-right:6px"></i>'
                f'{e(label)} ↗</button>')

    ctx_val = f"{tokens // 1000}k" if tokens else "?"
    ctx_sub = (f"{pct}% of {window // 1000}k" if pct is not None and window else "") + (f", compacts at {ctx['autoCompactsAtPercent']}%" if ctx.get("autoCompactsAtPercent") else "")
    out = [
        '<h2 class="sr-only">Context manager panel: context size, cache state, saved project state, and actions.</h2>',
        '<div style="padding:.5rem 0">',
        f'<div style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">{e(s["project"])} · {e(s.get("model") or "?")}</div>',
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">',
        metric("Context", ctx_val, ctx_sub),
        metric("Cache", cache.split(",")[0], cache.partition(", ")[2]),
        metric("Restore cost", f"~{s['digest_tokens']:,}", "tokens after reset"),
        metric("Saved state", f"{c['requirements']}R · {c['decisions']}D · {c['open_bugs']}B", "requirements, decisions, bugs"),
        '</div>',
    ]
    if plan:
        out.append('<div style="font-size:13px;color:var(--text-secondary);margin-top:10px">' +
                   " · ".join(f"{e(l)} {p}% used (resets in {e(r)})" for l, p, r in plan) + "</div>")
    out.append(f'<div style="margin-top:12px;padding:10px 12px;border-radius:var(--radius);background:var(--bg-{tone});color:var(--text-{tone});font-size:14px">{e(rec)}</div>')
    if s.get("task") or s.get("handoff"):
        t = s.get("task") or ""
        h = (s.get("handoff") or {}).get("text", "")
        out.append(f'<div style="font-size:13px;margin-top:10px;color:var(--text-secondary)">{"Task: " + e(t[:160]) if t else ""}'
                   f'{"<br>" if t and h else ""}{"Last handoff: " + e(h[:160]) + ("…" if len(h) > 160 else "") if h else ""}</div>')
    out += [
        '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px">',
        btn("Smart reset", "reset", "refresh-dot"),
        btn("Prepare model switch", "switch", "switch-horizontal"),
        btn("Save handoff only", "handoff", "device-floppy"),
        btn("Show digest", "digest", "file-text"),
        btn("Refresh", "panel", "reload"),
        '</div>',
    ]
    if s["open_bugs"]:
        out.append('<div style="margin-top:14px;font-size:13px;color:var(--text-secondary)">Open issues</div>')
        for b in s["open_bugs"][:5]:
            out.append(f'<div style="display:flex;gap:8px;align-items:center;border-bottom:0.5px solid var(--border);padding:6px 0;font-size:13px">'
                       f'<span style="flex:1;min-width:0;overflow-wrap:anywhere">[{e(b["id"])}] {e(b["text"][:140])}</span>'
                       f'<button data-a="resolve {e(b["id"])}" style="font-size:12px">Resolve ↗</button></div>')
    out += [
        '<div style="display:flex;gap:8px;margin-top:14px">',
        '<input id="q" placeholder="Search older history, decisions, outputs" style="flex:1;min-width:0">',
        '<button id="qs"><i class="ti ti-search" aria-hidden="true" style="font-size:16px;vertical-align:-2px"></i> Search ↗</button></div>',
        '<div style="display:flex;gap:8px;margin-top:8px">',
        '<select id="k">' + "".join(f"<option>{k}</option>" for k in ("decision", "requirement", "constraint", "bug", "note", "task")) + "</select>",
        '<input id="n" placeholder="Use Postgres, not SQLite" style="flex:1;min-width:0">',
        '<button id="ns"><i class="ti ti-plus" aria-hidden="true" style="font-size:16px;vertical-align:-2px"></i> Save ↗</button></div>',
        '<div id="err" style="font-size:13px;color:var(--text-danger);min-height:18px;margin-top:4px"></div>',
        '</div>',
        '<script>',
        'const go=a=>sendPrompt("ctx "+a);',
        'document.querySelectorAll("button[data-a]").forEach(b=>b.onclick=()=>go(b.dataset.a));',
        'const err=document.getElementById("err");',
        'const need=(id,msg)=>{const v=document.getElementById(id).value.trim();if(!v){err.textContent=msg;}return v;};',
        'document.getElementById("qs").onclick=()=>{const v=need("q","Enter something to search for");if(v)go("search "+v);};',
        'document.getElementById("ns").onclick=()=>{const v=need("n","Enter the note text first");if(v)go(document.getElementById("k").value+" "+JSON.stringify(v));};',
        'document.querySelectorAll("input").forEach(i=>i.oninput=()=>err.textContent="");',
        '</script>',
    ]
    return "\n".join(out)


def main_from_cli(status, usage_json):
    try:
        u = json.loads(usage_json) if usage_json else {}
    except ValueError:
        u = {}
    return render(status, u)
