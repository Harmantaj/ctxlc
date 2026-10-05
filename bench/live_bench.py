"""Live A/B benchmark: real `claude -p` sessions, baseline vs ctxlc.

All token numbers are read back from the session transcripts Claude Code
writes (per-request API usage), not estimated. "ITE" weights each field by
price relative to uncached input (read 0.1x, 1h write 2x, 5m write 1.25x,
output 5x).

Workloads
  tool      tool-heavy session: large build log, test output, repeated file reads
  cont-S/M/L  multi-day continuation: work session, then a retention quiz asked
            either by resuming the full transcript (baseline) or from a fresh
            session that receives the ctxlc digest (as after /clear)
  switch-M  model switch: work with haiku, quiz with sonnet (resume vs fresh+digest)
  compact   auto-compaction with a small window: native summary alone vs
            native summary + ctxlc state re-injection, then the quiz

usage: python3 bench/live_bench.py <workdir> [workload ...]
"""

import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ctxlc.ingest import transcripts_for_project  # noqa: E402
from ctxlc.report import usage_of_transcript  # noqa: E402

CTX = os.path.join(ROOT, "bin", "ctx")
TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]

GEN_PY = r'''import sys
mode = sys.argv[1]
if mode == "build":
    for i in range(4000):
        if i == 1234:
            print("src/ledger/money.c:88: error: implicit conversion from 'double' to 'int64_t' loses precision")
        elif i == 3001:
            print("src/cli/export.c:17: error: unknown type name 'csv_writer_t'")
        else:
            print(f"[{i:04d}/4000] CC src/mod_{i % 97}/unit_{i}.c -O2 ... ok")
    print("make: *** [all] Error 2")
    sys.exit(2)
if mode == "lint":
    for i in range(3000):
        if i in (512, 2048, 2900):
            print(f"src/mod_{i % 97}/unit_{i}.py:{i % 300}: W0612 unused variable 'tmp_{i}' (warning)")
        else:
            print(f"src/mod_{i % 97}/unit_{i}.py: checked, 0 issues")
    print("lint finished: 3 warnings, 0 errors")
    sys.exit(0)
if mode == "test":
    for i in range(520):
        if i == 404:
            print("tests/test_split.py::test_split_bill_rounding FAILED")
            print("  AssertionError: split_bill(100, 3) returned [33, 33, 33]; expected sum 100 (lost 1 cent)")
        elif i == 177:
            print("tests/test_io.py::test_import_unicode_names FAILED")
            print("  UnicodeDecodeError: 'ascii' codec can't decode byte 0xc3 in position 5")
        else:
            print(f"tests/test_mod_{i % 40}.py::test_case_{i} PASSED")
    print("=========== 2 failed, 518 passed in 12.34s ===========")
    sys.exit(1)
'''

LOREM = ("The ledger service reconciles shared expenses between members of a household. Each entry records a payer, "
         "a set of participants, an amount, a currency and a memo. Settlement proposals minimise the number of transfers. "
         "Historical entries are immutable; corrections are new entries that reference the original. ")


def doc(name, chars):
    body, i = [], 0
    while sum(map(len, body)) < chars:
        body.append(f"\n## Section {i} of {name}\n" + LOREM * 3)
        i += 1
    return f"# {name}\n" + "".join(body)


def config_yaml():
    lines = []
    for i in range(900):
        lines.append(f"setting_{i}: value_{i * 7 % 1000}  # tuning knob {i}")
        if i == 250:
            lines.append("retry_limit: 7")
        if i == 780:
            lines.append("timeout_ms: 4500")
    return "\n".join(lines) + "\n"


def setup(path, with_ctxlc, doc_chars=20000, n_docs=5, window=None):
    os.makedirs(os.path.join(path, "docs"), exist_ok=True)
    os.makedirs(os.path.join(path, "data"), exist_ok=True)
    with open(os.path.join(path, "gen.py"), "w") as f:
        f.write(GEN_PY)
    with open(os.path.join(path, "Makefile"), "w") as f:
        f.write("build:\n\t@python3 gen.py build\ntest:\n\t@python3 gen.py test\nlint:\n\t@python3 gen.py lint\n")
    with open(os.path.join(path, "data", "config.yaml"), "w") as f:
        f.write(config_yaml())
    for i in range(1, n_docs + 1):
        with open(os.path.join(path, "docs", f"design_{i}.md"), "w") as f:
            f.write(doc(f"design_{i}", doc_chars))
    settings = {}
    if with_ctxlc:
        subprocess.run([sys.executable, CTX, "install", "--project", path, "--no-window"], check=True, capture_output=True)
        with open(os.path.join(path, ".claude", "settings.local.json")) as f:
            settings = json.load(f)
    if window:
        settings.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    if settings:
        os.makedirs(os.path.join(path, ".claude"), exist_ok=True)
        with open(os.path.join(path, ".claude", "settings.local.json"), "w") as f:
            json.dump(settings, f, indent=2)


def turn(path, prompt, resume=None, model="haiku"):
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--allowedTools", *TOOLS]
    if resume:
        cmd += ["--resume", resume]
    t0 = time.time()
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=path, timeout=900)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f"claude failed: {p.stderr[:500]} {p.stdout[:500]}")
    out["_secs"] = time.time() - t0
    return out


def transcript(path, sid):
    for t in transcripts_for_project(path):
        if sid in t:
            return t
    raise FileNotFoundError(sid)


def ite(reqs):
    return sum(r["in"] + r["cw"] * r["ww"] + r["cr"] * 0.1 + r["out"] * 5 for r in reqs)


def usage_since(path, sid, start_idx=0):
    reqs, bnd, _ = usage_of_transcript(transcript(path, sid))
    part = reqs[start_idx:]
    return {
        "requests": len(part),
        "input": sum(r["in"] for r in part), "cache_write": sum(r["cw"] for r in part),
        "cache_read": sum(r["cr"] for r in part), "output": sum(r["out"] for r in part),
        "ite": round(ite(part)), "final_ctx": (part[-1]["in"] + part[-1]["cw"] + part[-1]["cr"]) if part else 0,
        "compactions": len([b for b in bnd if b >= start_idx]), "n_total": len(reqs),
    }


# --- workloads ------------------------------------------------------------------------------
TOOL_TURNS = [
    ("Run `make build` and tell me exactly which compile errors occurred (file:line and message).",
     ["money.c:88", "export.c:17"]),
    ("Run `make lint` and list every warning (file:line).", ["unit_512", "unit_2048", "unit_2900"]),
    ("Run `make test`. Which tests failed and why? One line each.",
     ["test_split_bill_rounding", "test_import_unicode_names"]),
    ("Read data/config.yaml and tell me the value of retry_limit.", ["7"]),
    ("Read data/config.yaml again (re-read it, don't rely on memory) and tell me the value of timeout_ms.", ["4500"]),
    ("Run `make build` once more to confirm the errors are unchanged, then summarise every finding so far in 5 bullets.",
     ["money.c", "export.c", "rounding", "unicode"]),
]

WORK_TURNS = [
    "We're building `ledgr`, a small bill-splitting CLI in Python. Requirements: the CLI must be named ledgr. "
    "Amounts must never be stored as floats — always integer cents. It must run fully offline, with no network access. "
    "For now we'll use SQLite for storage. Read docs/design_1.md for background, then reply with a 3-bullet summary.",
    "Read docs/design_2.md and docs/design_3.md and summarise the open questions in 3 bullets.",
    "Decision change: we're switching storage from SQLite to DuckDB instead, because of the analytics queries. "
    "Read docs/design_4.md and summarise it in 2 bullets.",
    "Run `make test` and tell me what failed.",
    "Don't fix the failing tests yet. The current task is: implement CSV export (`ledgr export --csv`). "
    "Read docs/design_5.md, then outline the export implementation in 4 bullets. Don't write code yet.",
]

QUIZ = ("Quick check before we continue — answer from what you know about this project (you may use tools to look things up, "
        "but do not read the docs/ files). Numbered list, one line each: 1) required CLI name? 2) how must money amounts be stored? "
        "3) which storage engine are we using now, and which one did we use before? 4) which failing test is about money/rounding, "
        "and what is wrong? 5) what is the current task? 6) any constraint about network access?")
QUIZ_KEYS = [["ledgr"], ["cent"], ["duckdb", "sqlite"], [r"split_bill|lost (1|one) cent|\[33"], ["csv"], ["offline|network"]]


def score(text, keys):
    low = text.lower()
    hits = [all(re.search(k, low) for k in group) for group in keys]
    return sum(hits), len(hits)


def run_tool(work, with_ctxlc, rep=""):
    path = os.path.join(work, f"tool{rep}-" + ("ctxlc" if with_ctxlc else "base"))
    setup(path, with_ctxlc, n_docs=0)
    sid, answers, correct, total = None, [], 0, 0
    for prompt, keys in TOOL_TURNS:
        out = turn(path, prompt, resume=sid)
        sid = out["session_id"]
        c, n = score(out["result"], [[k.lower()] for k in keys])
        correct, total = correct + c, total + n
        answers.append(out["result"])
    return {"arm": "ctxlc" if with_ctxlc else "baseline", "usage": usage_since(path, sid), "correct": f"{correct}/{total}", "path": path}


def work_session(path, model="haiku"):
    sid = None
    for p in WORK_TURNS:
        out = turn(path, p, resume=sid, model=model)
        sid = out["session_id"]
    return sid


def run_continuation(work, size, doc_chars, quiz_model="haiku", tag="cont"):
    """Both arms do the same work session; then baseline resumes it, ctxlc starts fresh with the digest."""
    res = {}
    for arm in ("baseline", "ctxlc"):
        path = os.path.join(work, f"{tag}-{size}-{arm}")
        setup(path, arm == "ctxlc", doc_chars=doc_chars)
        sid = work_session(path)
        work_usage = usage_since(path, sid)
        if arm == "baseline":
            q = turn(path, QUIZ, resume=sid, model=quiz_model)
            qu = usage_since(path, sid, start_idx=work_usage["n_total"])
        else:
            q = turn(path, QUIZ, model=quiz_model)  # fresh session == after /clear: SessionStart injects the digest
            qu = usage_since(path, q["session_id"])
        c, n = score(q["result"], QUIZ_KEYS)
        # Cold-cache view (resume after >TTL): every token of the first quiz request's prefix is re-written.
        res[arm] = {"work": work_usage, "quiz": qu, "retention": f"{c}/{n}", "answer": q["result"], "path": path,
                    "quiz_prefix_tokens": qu["final_ctx"] if qu["requests"] == 1 else None}
    return res


def run_compact(work, window=70000):
    res = {}
    for arm in ("baseline", "ctxlc"):
        path = os.path.join(work, f"compact-{arm}")
        setup(path, arm == "ctxlc", doc_chars=40000, window=window)
        sid = work_session(path)
        q = turn(path, QUIZ, resume=sid)
        u = usage_since(path, sid)
        c, n = score(q["result"], QUIZ_KEYS)
        res[arm] = {"usage": u, "retention": f"{c}/{n}", "answer": q["result"], "path": path}
    return res


def main():
    work = os.path.abspath(sys.argv[1])
    which = sys.argv[2:] or ["tool", "cont-S", "cont-M", "cont-L", "switch-M", "compact"]
    os.makedirs(work, exist_ok=True)
    results_path = os.path.join(work, "results.json")
    results = json.load(open(results_path)) if os.path.exists(results_path) else {}
    for w in which:
        t0 = time.time()
        if w.startswith("tool"):
            rep = w[4:]
            results[w] = {"baseline": run_tool(work, False, rep), "ctxlc": run_tool(work, True, rep)}
        elif w.startswith("cont-"):
            size = w[-1]
            results[w] = run_continuation(work, size, {"S": 3000, "M": 25000, "L": 80000}[size])
        elif w == "switch-M":
            results[w] = run_continuation(work, "M", 25000, quiz_model="sonnet", tag="switch")
        elif w == "compact":
            results[w] = run_compact(work)
        results[w]["_secs"] = round(time.time() - t0)
        with open(results_path, "w") as f:
            json.dump(results, f, indent=1)
        print(f"== {w} done in {results[w]['_secs']}s", flush=True)
    print(results_path)


if __name__ == "__main__":
    main()
