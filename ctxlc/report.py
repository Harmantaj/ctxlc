"""Token accounting from real API usage (transcripts) plus ctxlc's own metrics.

"ITE" = input-token equivalents: every usage field weighted by its price
relative to uncached input (cache read 0.1x, 5-minute cache write 1.25x,
1-hour cache write 2x, output 5x — Anthropic's published price structure).
"""

import json
from collections import defaultdict

from . import ingest, tokens

W_READ, W_OUT = 0.1, 5.0


def write_weight(usage):
    cc = usage.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens", 0), cc.get("ephemeral_1h_input_tokens", 0)
    total = w5 + w1
    return 2.0 if not total else (1.25 * w5 + 2.0 * w1) / total


def usage_of_transcript(path):
    """Main-thread requests in order: (tool_use_ids seen before this request, usage...)."""
    reqs, boundaries, pos_of_tool = [], [], {}
    with open(path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isSidechain"):
                continue
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                boundaries.append(len(reqs))
            if o.get("type") == "user":
                for b in (o.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        pos_of_tool[b.get("tool_use_id")] = len(reqs)
            if o.get("type") != "assistant":
                continue
            m = o.get("message") or {}
            u = m.get("usage")
            if not u or m.get("model") == "<synthetic>" or o.get("apiBlockIndex", 0) != 0:
                continue
            reqs.append({
                "model": m.get("model"), "ts": ingest.parse_ts(o.get("timestamp")),
                "in": u.get("input_tokens", 0), "cw": u.get("cache_creation_input_tokens", 0),
                "cr": u.get("cache_read_input_tokens", 0), "out": u.get("output_tokens", 0), "ww": write_weight(u),
            })
    return reqs, boundaries, pos_of_tool


def summarize(reqs):
    s = defaultdict(float)
    for r in reqs:
        s["requests"] += 1
        for k in ("in", "cw", "cr", "out"):
            s[k] += r[k]
        s["ite"] += r["in"] + r["cw"] * r["ww"] + r["cr"] * W_READ + r["out"] * W_OUT
        s["peak_ctx"] = max(s["peak_ctx"], r["in"] + r["cw"] + r["cr"])
    return dict(s)


def build(store):
    sessions = {}
    all_pos = {}
    for p in ingest.transcripts_for_project(store.project):
        reqs, bnd, pos = usage_of_transcript(p)
        if reqs:
            sid = p.rsplit("/", 1)[-1][:-6]
            sessions[sid] = dict(summarize(reqs), compactions=len(bnd))
            all_pos[sid] = (reqs, bnd, pos)
    tool = defaultdict(lambda: defaultdict(float))
    downstream = 0.0
    injected = 0
    guards = defaultdict(int)
    switches = []
    for m in store.iter_jsonl(store.metrics_path):
        ev = m.get("event")
        if ev == "tool":
            t = tool[m["kind"]]
            t["count"] += 1
            t["raw_tokens"] += tokens.estimate("x" * int(m.get("raw_chars", 0)))
            t["new_tokens"] += tokens.estimate("x" * int(m.get("new_chars", 0)))
            sess = all_pos.get(m.get("session"))
            if sess and not m.get("agent"):
                reqs, bnd, pos = sess
                at = pos.get(m.get("tool_use_id"))
                if at is not None:
                    end = min([b for b in bnd if b > at] + [len(reqs)])
                    saved = tokens.estimate("x" * int(m["raw_chars"] - m["new_chars"]))
                    # Each later request in the same context window would have re-read these tokens.
                    downstream += saved * ((end - at) * W_READ) + saved * 2.0  # + the one-time cache write
        elif ev == "inject":
            injected += tokens.estimate("x" * int(m.get("chars", 0)))
        elif ev in ("idle_guard_block", "idle_guard_override", "model_switch_request", "model_switch"):
            guards[ev] += 1
            if ev == "model_switch_request":
                switches.append(m)
    return {"sessions": sessions, "tool": {k: dict(v) for k, v in tool.items()}, "downstream_ite_saved": downstream,
            "injected_tokens": injected, "guards": dict(guards), "switches": switches}


def render(rep):
    out = ["# ctxlc report", "", "## Actual API usage (from transcripts)"]
    tot = defaultdict(float)
    out.append(f"{'session':38} {'req':>5} {'cache_write':>12} {'cache_read':>13} {'output':>9} {'peak_ctx':>9} {'ITE':>12}")
    for sid, s in sorted(rep["sessions"].items(), key=lambda kv: -kv[1]["ite"]):
        out.append(f"{sid:38} {int(s['requests']):5d} {int(s['cw']):12,d} {int(s['cr']):13,d} {int(s['out']):9,d} {int(s['peak_ctx']):9,d} {int(s['ite']):12,d}")
        for k in ("requests", "cw", "cr", "out", "ite"):
            tot[k] += s[k]
    out.append(f"{'TOTAL':38} {int(tot['requests']):5d} {int(tot['cw']):12,d} {int(tot['cr']):13,d} {int(tot['out']):9,d} {'':9} {int(tot['ite']):12,d}")
    out += ["", "## Tool-output optimization (L1)"]
    for k, t in rep["tool"].items():
        red = 1 - t["new_tokens"] / t["raw_tokens"] if t["raw_tokens"] else 0
        out.append(f"{k:14} n={int(t['count'])}  ~{int(t['raw_tokens']):,} → ~{int(t['new_tokens']):,} tokens ({red:.0%} removed)")
    out.append(f"Downstream saving from those removals (counted against the actual later requests in each context window): ~{int(rep['downstream_ite_saved']):,} ITE")
    out += ["", "## State injection & guards",
            f"Digest/protocol tokens injected: ~{rep['injected_tokens']:,}",
            f"Guard events: {rep['guards'] or 'none'}"]
    return "\n".join(out)
