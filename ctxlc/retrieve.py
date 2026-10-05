"""L3: on-demand retrieval of history that is no longer in the active context.

Pure-Python BM25 over history.jsonl entries, state items (including superseded
ones, clearly labelled) and archived tool outputs. Runs locally: retrieval costs
only the tokens of the snippets actually returned.
"""

import math
import os
import re
from collections import Counter

from .store import fmt_ts

TOKEN = re.compile(r"[a-z0-9_]{2,}")


def toks(text):
    return TOKEN.findall(text.lower())


def corpus(store, st):
    docs = []
    for i, rec in enumerate(store.iter_jsonl(store.history_path)):
        label = rec.get("kind", "?") + (":" + rec["tool"] if rec.get("tool") else "")
        docs.append({"ref": f"h{i}", "ts": rec.get("ts"), "label": label, "text": rec.get("text", "")})
    for it in st["items"]:
        docs.append({
            "ref": it["id"], "ts": it["updated"], "label": f"{it['kind']} [{it['status'].upper()}]",
            "text": it["text"] + (f" (superseded by {it['superseded_by']})" if it.get("superseded_by") else ""),
        })
    try:
        names = sorted(os.listdir(store.artifacts))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(store.artifacts, name)
        try:
            with open(path, errors="replace") as f:
                text = f.read(200_000)
        except OSError:
            continue
        docs.append({"ref": "a:" + name[:-4], "ts": os.path.getmtime(path), "label": "archived output", "text": text})
    return docs


def search(store, st, query, k=6, snippet=320):
    docs = corpus(store, st)
    q = toks(query)
    if not q or not docs:
        return []
    tfs = [Counter(toks(d["text"])) for d in docs]
    lens = [sum(tf.values()) or 1 for tf in tfs]
    avg = sum(lens) / len(lens)
    df = Counter(t for tf in tfs for t in set(tf) if t in q)
    N = len(docs)
    scored = []
    for d, tf, ln in zip(docs, tfs, lens):
        s = 0.0
        for t in q:
            if tf.get(t):
                idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * ln / avg))
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: (-x[0], -(x[1]["ts"] or 0)))
    out = []
    for s, d in scored[:k]:
        out.append(dict(d, score=round(s, 2), snippet=_snippet(d["text"], q, snippet)))
    return out


def _snippet(text, q, n):
    low = text.lower()
    pos = min((low.find(t) for t in q if low.find(t) >= 0), default=0)
    start = max(0, pos - n // 3)
    s = " ".join(text[start:start + n].split())
    return ("…" if start else "") + s + ("…" if start + n < len(text) else "")


def show(store, st, ref, grep=None, lines=None, max_chars=12000):
    text = None
    if (ref.startswith("h") and ref[1:].isdigit()) or ref.startswith("u:"):
        idx = int(ref[1:]) if ref[0] == "h" else None
        for i, rec in enumerate(store.iter_jsonl(store.history_path)):
            if i == idx or (idx is None and rec.get("uuid") == ref[2:]):
                text = f"[{rec.get('kind')} {fmt_ts(rec.get('ts'))} session={rec.get('session')}]\n{rec.get('text', '')}"
                break
    elif ref.startswith("a:"):
        path = os.path.join(store.artifacts, ref[2:] + ".log")
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                text = f.read()
    else:
        for it in st["items"]:
            if it["id"].lower() == ref.lower():
                text = "\n".join(f"{k}: {v}" for k, v in it.items())
    if text is None:
        return f"no such ref: {ref}"
    body = text.splitlines()
    if grep:
        rx = re.compile(grep, re.I)
        body = [f"{i + 1}: {l}" for i, l in enumerate(body) if rx.search(l)]
    if lines:
        a, _, b = lines.partition(":")
        body = body[int(a or 1) - 1:int(b) if b else None]
    out = "\n".join(body)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n… [{len(out) - max_chars:,} more chars; narrow with --grep or --lines]"
    return out
