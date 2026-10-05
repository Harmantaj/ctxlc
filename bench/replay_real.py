"""Offline replay of real Claude Code transcripts through ctxlc.

For every transcript under ~/.claude/projects (or the paths given):
  * L2: ingest into a scratch store, time it, and compare the rendered digest
    with the session's actual final context size (from API usage).
  * L1: run every real Bash / Read result through the same compaction/dedup
    code the PostToolUse hook uses, and credit the removed tokens against the
    *actual* number of later requests in that context window (each re-reads
    them from cache at 0.1x, plus the one-time 2x cache write).

Nothing here calls a model. Usage numbers come from the transcripts.
"""

import glob
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxlc import config, ingest, tokens, toolout  # noqa: E402
from ctxlc.report import usage_of_transcript, write_weight  # noqa: E402,F401
from ctxlc.store import Store, empty_state, render_digest  # noqa: E402


def replay(path, tmp):
    store = Store(tmp)
    cfg = config.load(store.dir)
    st = empty_state(tmp)
    t0 = time.perf_counter()
    ingest.ingest_transcript(store, st, path)
    t_ingest = time.perf_counter() - t0
    digest = render_digest(st, cfg["digest_budget_tokens"], cfg["recent_prompts_kept"])
    reqs, bounds, pos = usage_of_transcript(path)
    if not reqs:
        return None
    final_ctx = reqs[-1]["in"] + reqs[-1]["cw"] + reqs[-1]["cr"]

    # L1 replay over real tool results
    names, inputs = {}, {}
    epoch = {"outputs": {}, "reads": {}}
    removed_tok, saved_ite, n_rewrites, raw_tool_tok = 0, 0.0, 0, 0
    with open(path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isSidechain"):
                continue
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                epoch = {"outputs": {}, "reads": {}}
            m = o.get("message") or {}
            if o.get("type") == "assistant":
                for b in m.get("content") or []:
                    if b.get("type") == "tool_use":
                        names[b["id"]], inputs[b["id"]] = b["name"], b.get("input") or {}
            if o.get("type") != "user" or not isinstance(m.get("content"), list):
                continue
            for b in m["content"]:
                if b.get("type") != "tool_result" or b.get("is_error"):
                    continue
                tid = b.get("tool_use_id")
                text = ingest.tool_result_text(b)
                raw_tool_tok += tokens.estimate(text)
                if names.get(tid) == "Bash":
                    new, met = toolout.compact_bash(store, cfg, epoch, inputs[tid], {"stdout": text, "stderr": ""})
                elif names.get(tid) == "Read":
                    body = "\n".join(l.split("\t", 1)[-1] for l in text.splitlines())  # strip cat -n prefixes
                    resp = {"type": "text", "file": {"filePath": inputs[tid].get("file_path"), "content": body}}
                    new, met = toolout.dedup_read(cfg, epoch, inputs[tid], resp)
                else:
                    continue
                if not new:
                    continue
                n_rewrites += 1
                cut = tokens.estimate("x" * max(0, met["raw_chars"] - met["new_chars"]))
                removed_tok += cut
                at = pos.get(tid)
                if at is not None:
                    end = min([x for x in bounds if x > at] + [len(reqs)])
                    saved_ite += cut * (end - at) * 0.1 + cut * 2.0
    total_ite = sum(r["in"] + r["cw"] * r["ww"] + r["cr"] * 0.1 + r["out"] * 5 for r in reqs)
    return {
        "session": os.path.basename(path)[:8],
        "requests": len(reqs),
        "final_ctx": final_ctx,
        "digest_tok": tokens.estimate(digest),
        "ingest_s": t_ingest,
        "items": len(st["items"]),
        "l1_rewrites": n_rewrites,
        "l1_removed_tok": removed_tok,
        "l1_saved_ite": saved_ite,
        "total_ite": total_ite,
        "raw_tool_tok": raw_tool_tok,
    }


def main():
    paths = sys.argv[1:] or glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    rows = []
    for p in paths:
        with tempfile.TemporaryDirectory() as tmp:
            r = replay(p, tmp)
        if r and r["requests"] >= 5:
            rows.append(r)
    rows.sort(key=lambda r: -r["total_ite"])
    print(f"{'session':9} {'req':>5} {'final_ctx':>10} {'digest':>7} {'ratio':>6} {'ingest':>7} {'L1 n':>5} {'L1 cut':>8} {'L1 ITE saved':>13} {'of total':>9}")
    for r in rows:
        print(f"{r['session']:9} {r['requests']:5d} {r['final_ctx']:10,d} {r['digest_tok']:7,d} {r['final_ctx'] / max(1, r['digest_tok']):5.0f}x "
              f"{r['ingest_s']:6.2f}s {r['l1_rewrites']:5d} {r['l1_removed_tok']:8,d} {int(r['l1_saved_ite']):13,d} {r['l1_saved_ite'] / r['total_ite']:8.1%}")
    T = sum(r["total_ite"] for r in rows)
    S = sum(r["l1_saved_ite"] for r in rows)
    print(f"\n{len(rows)} sessions. L1 tool-output saving: {S / 1e6:.2f}M of {T / 1e6:.1f}M ITE ({S / T:.1%}).")
    print(f"Median digest {sorted(r['digest_tok'] for r in rows)[len(rows) // 2]:,} tok vs median final context "
          f"{sorted(r['final_ctx'] for r in rows)[len(rows) // 2]:,} tok. Max ingest time {max(r['ingest_s'] for r in rows):.2f}s.")


if __name__ == "__main__":
    main()
