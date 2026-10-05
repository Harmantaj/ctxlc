"""Past conversations of a project, readable after /clear.

Claude Code keeps every conversation's transcript under ~/.claude/projects/, /clear included; the app just stops
showing it. `ctx history` lists them and renders one as a standalone HTML page: prompts and replies in full, tool
calls folded to one line each, so the old chat can be read next to the new one.
"""

import html
import json
import os
import time

from . import ingest


def _records(path):
    with open(path, errors="ignore", encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def conversations(project):
    """Newest first: {id, path, start, end, prompts, title, first}."""
    out = []
    for path in ingest.transcripts_for_project(project):
        c = {"id": os.path.basename(path)[:-6], "path": path, "start": None, "end": None, "prompts": 0,
             "title": None, "first": None}
        for o in _records(path):
            if o.get("type") == "custom-title":
                c["title"] = o.get("customTitle") or c["title"]
            if o.get("type") not in ("user", "assistant") or o.get("isSidechain"):
                continue
            ts = ingest.parse_ts(o.get("timestamp"))
            if ts:
                c["start"] = c["start"] or ts
                c["end"] = ts
            if o["type"] == "user" and not (o.get("isMeta") or o.get("isCompactSummary")):
                text = ingest.clean_prompt((o.get("message") or {}).get("content"))
                if text and not ingest.CONTROL.match(text):
                    c["prompts"] += 1
                    c["first"] = c["first"] or text
        if c["prompts"]:
            out.append(c)
    return sorted(out, key=lambda c: c["end"] or 0, reverse=True)


def find(project, ref):
    """ref is a list number (1 = newest) or a session-id prefix."""
    convs = conversations(project)
    if ref.isdigit() and 0 < int(ref) <= len(convs):
        return convs[int(ref) - 1]
    hits = [c for c in convs if c["id"].startswith(ref)]
    return hits[0] if len(hits) == 1 else None


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def _tool_line(b):
    inp = b.get("input") or {}
    arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or inp.get("description") or ""
    return f"{b.get('name', 'tool')}: {str(arg).splitlines()[0][:160] if arg else ''}"


def turns(c):
    """[(kind, when, text)] with kind you|claude|tools|compact; tool calls folded per run."""
    out, tools = [], []

    def flush():
        if tools:
            out.append(("tools", None, "\n".join(tools)))
            tools.clear()

    for o in _records(c["path"]):
        if o.get("type") not in ("user", "assistant") or o.get("isSidechain"):
            continue
        content = (o.get("message") or {}).get("content")
        when = _when(ingest.parse_ts(o.get("timestamp")))
        if o["type"] == "user":
            if o.get("isCompactSummary"):
                flush()
                out.append(("compact", when, ""))
                continue
            text = None if o.get("isMeta") else ingest.clean_prompt(content)
            if text:
                flush()
                out.append(("you", when, text))
            continue
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use":
                tools.append(_tool_line(b))
            elif b.get("type") == "text" and b.get("text", "").strip():
                flush()
                out.append(("claude", when, b["text"].strip()))
    flush()
    return out


def markdown(c):
    lines = [f"Conversation {c['id'][:8]} · {_when(c['start'])} – {_when(c['end'])} · {c['prompts']} prompts", ""]
    for kind, when, text in turns(c):
        if kind == "tools":
            n = text.count("\n") + 1
            lines += [f"_{n} tool call{'s' * (n > 1)}_", ""]
        elif kind == "compact":
            lines += [f"--- _compacted here · {when}_ ---", ""]
        else:
            lines += [f"**{'You' if kind == 'you' else 'Claude'}** · {when}", "", text, ""]
    return "\n".join(lines)


def render(c):
    e = lambda s: html.escape(str(s), quote=False)
    parts = []
    for kind, when, text in turns(c):
        if kind == "tools":
            rows = text.split("\n")
            parts.append(f'<details class="tools"><summary>{len(rows)} tool call{"s" * (len(rows) > 1)}</summary>'
                         + "".join(f"<div>{e(t)}</div>" for t in rows) + "</details>")
        elif kind == "compact":
            parts.append(f'<div class="note">Conversation compacted here · {when}</div>')
        else:
            who = "You" if kind == "you" else "Claude"
            parts.append(f'<div class="msg {kind}"><div class="who">{who} · {when}</div><div class="body">{e(text)}</div></div>')
    turns_html = "".join(parts)
    title = c["title"] or (c["first"] or "Conversation")[:80]
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>
:root{{--bg:#faf9f7;--fg:#1f1e1c;--muted:#6b6862;--you:#efece6;--line:#e2ded6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b19;--fg:#ecebe8;--muted:#a19d95;--you:#2a2926;--line:#34322e}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:20px;margin:0 0 4px}} .meta{{color:var(--muted);font-size:13px;margin-bottom:24px}}
.msg{{margin:14px 0}} .who{{font-size:12px;color:var(--muted);margin-bottom:4px}}
.body{{white-space:pre-wrap;overflow-wrap:anywhere}} .you .body{{background:var(--you);padding:10px 12px;border-radius:10px}}
.tools{{font-size:13px;color:var(--muted);margin:6px 0}} .tools div{{font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere;padding:2px 0}}
.note{{text-align:center;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:6px;margin:20px 0}}
</style></head><body><main><h1>{e(title)}</h1>
<div class="meta">{_when(c["start"])} – {_when(c["end"])} · {c["prompts"]} prompts · session {e(c["id"])}</div>
{turns_html}</main></body></html>"""
