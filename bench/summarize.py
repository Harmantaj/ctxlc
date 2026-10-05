"""Render bench/live_bench.py results.json as markdown tables.

Measured = read from the transcripts' API usage. The "cold" column for
continuation is derived: when the resume happens after the cache TTL, the whole
prefix of the first request is written at the 1h cache-write price (2x) instead
of read (0.1x); that is what the audit of real sessions showed happens.
"""

import json
import sys


def pct(new, old):
    return f"{(new - old) / old:+.0%}" if old else "n/a"


def main(path):
    r = json.load(open(path))
    out = ["### Tool-heavy session (5 turns: failing build, 200k-char lint, failing tests, file read twice, re-run)", "",
           "| run | baseline ITE | ctxlc ITE | Δ | requests (b / c) | final context (b / c) | correct (b / c) |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    tb = tc = 0
    for k in sorted(x for x in r if x.startswith("tool")):
        b, c = r[k]["baseline"], r[k]["ctxlc"]
        tb += b["usage"]["ite"]
        tc += c["usage"]["ite"]
        out.append(f"| {k} | {b['usage']['ite']:,} | {c['usage']['ite']:,} | {pct(c['usage']['ite'], b['usage']['ite'])} | "
                   f"{b['usage']['requests']} / {c['usage']['requests']} | {b['usage']['final_ctx']:,} / {c['usage']['final_ctx']:,} | "
                   f"{b['correct']} / {c['correct']} |")
    if tb:
        out.append(f"| **total** | **{tb:,}** | **{tc:,}** | **{pct(tc, tb)}** | | | |")

    out += ["", "### Continuation after a break (same work session; then baseline resumes the full transcript, ctxlc starts fresh with the digest)", "",
            "| workload | context before quiz (b) | quiz prefix (c) | quiz ITE warm (b / c) | quiz ITE cold, derived (b / c) | retention (b / c) |",
            "|---|---:|---:|---:|---:|---:|"]
    for k in sorted(x for x in r if x.startswith(("cont", "switch"))):
        b, c = r[k]["baseline"], r[k]["ctxlc"]
        bq, cq = b["quiz"], c["quiz"]

        def cold(q):
            # First request: whole prefix written at 2x; later requests in the quiz turn unchanged.
            first_prefix = q["final_ctx"] if q["requests"] == 1 else None
            return round(q["ite"] - q["cache_read"] * 0.1 + q["cache_read"] * 2.0) if first_prefix else q["ite"]

        out.append(f"| {k} | {b['work']['final_ctx']:,} | {cq['final_ctx']:,} | {bq['ite']:,} / {cq['ite']:,} | "
                   f"{cold(bq):,} / {cold(cq):,} ({pct(cold(cq), cold(bq))}) | {b['retention']} / {c['retention']} |")
    if "compact" in r:
        b, c = r["compact"]["baseline"], r["compact"]["ctxlc"]
        out += ["", "### Auto-compaction (window forced to 70k): native summary vs native summary + ctxlc state", "",
                "| arm | compactions | total ITE | retention |", "|---|---:|---:|---:|",
                f"| baseline | {b['usage']['compactions']} | {b['usage']['ite']:,} | {b['retention']} |",
                f"| ctxlc | {c['usage']['compactions']} | {c['usage']['ite']:,} | {c['retention']} |"]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
