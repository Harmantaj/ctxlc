"""Trace-driven replay of real sessions under different trigger policies.

Input: every main-thread API request recorded in ~/.claude/projects/*/*.jsonl
(timestamp, model, uncached input, cache write, cache read). The context growth
between requests is taken from the real trace; the policy decides when the
context is shrunk:

  W  auto-compact window: when the context exceeds W, compact. Cost: one
     request that reads the context (0.1x) and writes a summary (5x), after
     which the context is floor + summary + a re-read allowance.
  R  state restore: when the cache is cold (idle > TTL, or model switch) and
     the context exceeds R, the user takes the /clear suggestion; the new
     context is floor + digest + re-read allowance (instead of re-caching all).

Costs are input-token equivalents (ITE: read 0.1x, 1h write 2x, output 5x).
Savings are reported against the same simulator with no policy, so modelling
error cancels out. This is a simulation over real traces, not a measurement.
"""

import datetime
import glob
import json
import os
import sys

CW, CR, IN, OUT = 2.0, 0.1, 1.0, 5.0
TTL = 3600


def load_traces():
    def ts(s):
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    traces = []
    for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        seq = []
        for line in open(f):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isSidechain") or o.get("type") != "assistant":
                continue
            m = o.get("message") or {}
            u = m.get("usage")
            if not u or m.get("model") == "<synthetic>" or o.get("apiBlockIndex", 0) != 0:
                continue
            seq.append({"t": ts(o["timestamp"]), "model": m["model"], "i": u.get("input_tokens", 0),
                        "cw": u.get("cache_creation_input_tokens", 0), "cr": u.get("cache_read_input_tokens", 0)})
        if len(seq) >= 5:
            traces.append(seq)
    return traces


def simulate(seq, W=None, R=None, digest=2500, summary=4000, reread=8000):
    floor = min(r["i"] + r["cw"] + r["cr"] for r in seq)
    c = pctx = None
    cost, comps, restores, prev = 0.0, 0, 0, None
    for r in seq:
        ctx = r["i"] + r["cw"] + r["cr"]
        if c is None:
            c = pctx = ctx
            cost += ctx * CW
            prev = r
            continue
        d = max(0, ctx - pctx)
        pctx = ctx
        cold = r["t"] - prev["t"] > TTL or r["model"] != prev["model"]
        if cold:
            if R is not None and c > R:
                c = floor + digest + reread
                restores += 1
            cost += c * CW + d * CW
            c += d
        else:
            cost += c * CR + d * CW
            c += d
        if W and c > W:
            cost += c * CR + summary * OUT
            c = floor + summary + reread
            cost += c * CW
            comps += 1
        prev = r
    return cost, comps, restores


def main():
    traces = load_traces()
    base = sum(simulate(s)[0] for s in traces)
    print(f"{len(traces)} real sessions, {sum(map(len, traces)):,} requests. No-policy replay: {base / 1e6:.1f}M ITE\n")
    print(f"{'compact window':>15} {'restore >':>10} {'ITE':>9} {'saving':>7} {'compactions':>12} {'restores':>9}")
    rows = []
    for W in (None, 400e3, 250e3, 200e3, 160e3, 120e3, 100e3, 80e3):
        for R in (None, 100e3, 60e3):
            tot = comps = res = 0
            for s in traces:
                c, a, b = simulate(s, W, R)
                tot, comps, res = tot + c, comps + a, res + b
            rows.append((W, R, tot))
            print(f"{(str(int(W / 1e3)) + 'k') if W else 'off':>15} {(str(int(R / 1e3)) + 'k') if R else 'off':>10} "
                  f"{tot / 1e6:8.1f}M {1 - tot / base:7.1%} {comps:12d} {res:9d}")
    best = min(rows, key=lambda r: r[2])
    print(f"\nbest: window={best[0]} restore>{best[1]} saving {1 - best[2] / base:.1%}")
    if "--sensitivity" in sys.argv:
        for rr in (0, 8000, 20000, 40000):
            tot = sum(simulate(s, 160e3, 60e3, reread=rr)[0] for s in traces)
            print(f"re-read allowance {rr:>6}: saving {1 - tot / base:.1%}")


if __name__ == "__main__":
    main()
