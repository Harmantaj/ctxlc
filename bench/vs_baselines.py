"""ctxlc vs the simple alternatives, from the *same* conversation.

One work session runs with no context management at all. Each arm then continues
from that identical history (forked with --fork-session) and answers the same
13-question retention quiz:

  resume    keep the full conversation (reference: nothing can be lost)
  compact   native /compact, then continue
  handoff   the model writes HANDOFF.md, then a fresh session reads it
  ctxlc     fresh session; ctxlc's digest is injected automatically (no model work)
  ctxlc+ho  ctxlc smart reset: the model saves a short handoff via `ctx handoff`,
            then a fresh session gets digest + handoff

The quiz includes traps: a superseded decision, a bug fixed mid-conversation that
must not be reported as open, a casually stated preference, a rejected approach,
and an exact location that only ever appeared in command output.

Cost is Claude Code's own per-invocation total_cost_usd (as run: warm cache).
"cold ITE" re-prices, from the logged usage, what the same steps would cost after a
break longer than the cache TTL: the first request of each step pays its whole
prefix as a 1h cache write (2x) instead of a read (0.1x). ITE = input-token
equivalents (in 1x, read 0.1x, 1h write 2x, output 5x).

usage: python3 bench/vs_baselines.py <workdir> [reps] [model] [scenario]
  model: haiku (default), sonnet, or a full model ID
  scenario: ledgr (default, defined here) or relay (bench/scenario_relay.py)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))
from live_bench import setup, transcript, CTX, TOOLS  # noqa: E402
from ctxlc.ingest import transcripts_for_project  # noqa: E402
from ctxlc.report import usage_of_transcript  # noqa: E402

MODEL = "haiku"

WORK = [
    "We're building `ledgr`, a bill-splitting CLI in Python. Requirements: the CLI must be named ledgr. Amounts must "
    "never be stored as floats — always integer cents. It must run fully offline, with no network access. We'll use "
    "SQLite for storage for now. Read docs/design_1.md for background, then reply with a 3-bullet summary.",
    "Read docs/design_2.md and summarise the open questions in 3 bullets. Oh, and personally I can't stand pandas — "
    "keep it out of this project, it's way too heavy for a CLI.",
    "Run `make build` and tell me which compile errors occurred (file:line and message).",
    "Decision change: we're switching storage from SQLite to DuckDB, because of the analytics queries. "
    "Read docs/design_3.md and summarise it in 2 bullets.",
    "Run `make test` and tell me what failed.",
    "I just patched the unicode import bug myself on my machine, so that failure is fixed — only the rounding failure "
    "is still open. Read docs/design_4.md and give me 2 bullets.",
    "About the rounding bug: we tried Python's built-in round() in split_bill and it's rejected — banker's rounding "
    "loses cents. Use the largest-remainder method instead. Don't implement it yet. Also, Priya will review the PR on Friday.",
    "The current task is CSV export (`ledgr export --csv`). The CSV columns must be date,payer,amount_cents,memo in "
    "exactly that order. Read docs/design_5.md, then outline the export implementation in 4 bullets. Don't write code yet.",
]

QUIZ = (
    "Quick check before we continue. Answer from what you know about this project. You may use tools, but do not run "
    "make and do not open files under docs/ or data/. Reply as a numbered list, one short line each:\n"
    "1) What must the CLI be named?\n2) How must money amounts be stored?\n3) Which storage engine are we using now?\n"
    "4) Which storage engine did we use before, and why did we switch?\n"
    "5) Is there any library I asked you to keep out of the project?\n6) Any constraint about network access?\n"
    "7) Which test failures are still open right now?\n8) Which approach to the rounding bug was rejected, and why?\n"
    "9) Which approach should we use instead?\n10) What is the current task?\n"
    "11) What exact column order must the CSV have?\n"
    "12) In `make build`, which file:line had the double-to-int64_t conversion error?\n"
    "13) Who reviews the PR, and when?"
)


def _has(*pats):
    return lambda a: all(re.search(p, a) for p in pats)


CHECKS = {
    1: ("CLI name", _has(r"ledgr")),
    2: ("integer cents", _has(r"cent")),
    3: ("current storage (superseded decision)", lambda a: "duckdb" in a and (a.find("sqlite") < 0 or a.find("duckdb") < a.find("sqlite"))),
    4: ("previous storage + reason", _has(r"sqlite", r"analytic")),
    5: ("casual preference: no pandas", _has(r"pandas")),
    6: ("offline constraint", _has(r"offline|no network|without (any )?network|network access")),
    7: ("open bugs (unicode was fixed)", lambda a: re.search(r"round|split_bill", a)
        and (not re.search(r"unicode", a) or re.search(r"fixed|patched|resolved|closed|not open|no longer", a))),
    8: ("rejected approach + reason", _has(r"round\(|built-?in round|round\b", r"banker|lose|lost|loses")),
    9: ("chosen approach", _has(r"largest[ -]?remainder")),
    10: ("current task", _has(r"csv|export")),
    11: ("exact CSV column order", _has(r"date\W{1,5}payer\W{1,5}amount_cents\W{1,5}memo")),
    12: ("detail only in command output", _has(r"money\.c\D{0,3}88")),
    13: ("reviewer + day", _has(r"priya", r"friday")),
}

HANDOFF_PROMPT = (
    "We'll continue this work in a brand-new session that will NOT see this conversation. Write HANDOFF.md in the "
    "project root with everything the next session needs to continue seamlessly: requirements, constraints, current "
    "decisions, open bugs, the current task and its details, preferences, and anything else important. Be thorough "
    "but concise.")
CTX_HANDOFF_PROMPT = (
    f"Smart reset (ctxlc). A fresh context will continue this work; it automatically receives ctxlc's digest "
    f"(requirements, decisions, task, open bugs, recent messages). Write a 5-15 line handoff with what it needs that "
    f"the digest would not otherwise carry — what is in progress, the exact next step, open questions, gotchas, "
    f"details that only appeared in conversation. Save it by running: \"{CTX}\" handoff - <<'EOF' … EOF")
CTX_HANDOFF_PROMPT_TOOLS = TOOLS


def claude(path, prompt, resume=None, fork=False):
    cmd = ["claude", "-p", "--model", MODEL, "--output-format", "json", "--setting-sources", "local",
           "--allowedTools", *TOOLS]
    if resume:
        cmd += ["--resume", resume] + (["--fork-session"] if fork else [])
    # Auto-memory would let every arm recall preferences it saved during the work session,
    # which hides what each method itself carries forward.
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=path, timeout=900, env=env)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f"claude failed: {p.stderr[:500]} {p.stdout[:500]}")


def reqs_of(path, sid):
    return usage_of_transcript(transcript(path, sid))[0]


def ctx_of(r):
    return r["in"] + r["cw"] + r["cr"]


def cold_ite(reqs):
    """ITE if this step started after the cache expired: the first request writes its whole prefix."""
    tot = 0.0
    for i, r in enumerate(reqs):
        if i == 0:
            tot += (r["cr"] + r["cw"]) * 2.0 + r["in"]
        else:
            tot += r["in"] + r["cw"] * r["ww"] + r["cr"] * 0.1
        tot += r["out"] * 5
    return tot


def score(answer):
    items = {}
    for m in re.finditer(r"(?m)^\W{0,4}(\d{1,2})[\).:]\s*(.*(?:\n(?!\W{0,4}\d{1,2}[\).:]).*)*)", answer):
        items.setdefault(int(m.group(1)), m.group(2).lower())
    res = {q: bool(fn(items.get(q, ""))) for q, (_, fn) in CHECKS.items()}
    return res


def archive_transcripts(path, keep, dest):
    os.makedirs(dest, exist_ok=True)
    for t in transcripts_for_project(path):
        if not any(k in t for k in keep):
            shutil.move(t, dest)


def one_rep(work, rep):
    path = os.path.join(work, f"rep{rep}")
    if os.path.exists(path):
        shutil.rmtree(path)
    for t in transcripts_for_project(path):
        os.remove(t)
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))
    shutil.rmtree(os.path.join(os.path.expanduser("~/.claude/projects"), slug, "memory"), ignore_errors=True)
    setup(path, False, doc_chars=25000, n_docs=5)
    arch = os.path.join(work, f"rep{rep}-archive")
    out = {"arms": {}}

    sid = None
    cost = 0.0
    for p in WORK:
        o = claude(path, p, resume=sid)
        sid, cost = o["session_id"], cost + o.get("total_cost_usd", 0)
    W = reqs_of(path, sid)
    nW = len(W)
    out["work"] = {"requests": nW, "final_context": ctx_of(W[-1]), "usd": round(cost, 4)}

    def record(name, transition, quiz, prefix_reqs, transition_reqs, extra=None):
        q_reqs = prefix_reqs
        out["arms"][name] = {
            "transition_usd": round(transition.get("total_cost_usd", 0), 4) if transition else 0.0,
            "quiz_usd": round(quiz.get("total_cost_usd", 0), 4),
            "new_context_tokens": ctx_of(q_reqs[0]) if q_reqs else None,
            "cold_ite": round(cold_ite(transition_reqs) + cold_ite(q_reqs)) if q_reqs else None,
            "score": score(quiz["result"]), "answer": quiz["result"], **(extra or {}),
        }
        print(f"rep{rep} {name}: {sum(out['arms'][name]['score'].values())}/{len(CHECKS)}", flush=True)

    # ctxlc (automatic): install now, so the work session itself ran unmanaged like every other arm.
    subprocess.run([sys.executable, CTX, "install", "--project", path, "--no-window", "--no-skill"], check=True,
                   capture_output=True)
    q = claude(path, QUIZ)
    digest = subprocess.run([sys.executable, CTX, "digest"], cwd=path, capture_output=True, text=True).stdout
    record("ctxlc", None, q, reqs_of(path, q["session_id"]), [], {"digest_chars": len(digest)})
    with open(os.path.join(work, f"rep{rep}-digest.md"), "w") as f:
        f.write(digest)
    archive_transcripts(path, [sid], arch)
    shutil.rmtree(os.path.join(path, ".claude", "context"))

    # ctxlc smart reset: model saves a handoff in the old context, fresh session gets digest + handoff.
    t = claude(path, CTX_HANDOFF_PROMPT, resume=sid, fork=True)
    t_reqs = reqs_of(path, t["session_id"])[nW:]
    q = claude(path, QUIZ)
    record("ctxlc+handoff", t, q, reqs_of(path, q["session_id"]), t_reqs)
    archive_transcripts(path, [sid], arch)
    subprocess.run([sys.executable, CTX, "uninstall", "--project", path, "--no-skill"], check=True, capture_output=True)
    shutil.rmtree(os.path.join(path, ".claude", "context"))

    # resume: the full conversation (reference).
    q = claude(path, QUIZ, resume=sid, fork=True)
    record("resume", None, q, reqs_of(path, q["session_id"])[nW:], [])

    # native /compact.
    t = claude(path, "/compact", resume=sid, fork=True)
    q = claude(path, QUIZ, resume=t["session_id"])
    reqs = reqs_of(path, t["session_id"])
    q_reqs = reqs[nW:]
    # The compaction request itself is not logged with usage; it reads the whole conversation once.
    summary_out = 0
    for line in open(transcript(path, t["session_id"])):
        o = json.loads(line)
        if o.get("isCompactSummary"):
            c = o["message"]["content"]
            summary_out = len(c if isinstance(c, str) else json.dumps(c)) // 4
    compact_cold = ctx_of(W[-1]) * 2.0 + summary_out * 5
    record("compact", t, q, q_reqs, [], {"summary_tokens": summary_out})
    out["arms"]["compact"]["cold_ite"] = round(compact_cold + cold_ite(q_reqs))

    # simple handoff file.
    t = claude(path, HANDOFF_PROMPT, resume=sid, fork=True)
    t_reqs = reqs_of(path, t["session_id"])[nW:]
    q = claude(path, "Read HANDOFF.md first — it is the handoff from the previous session. Then:\n" + QUIZ)
    hand = open(os.path.join(path, "HANDOFF.md")).read() if os.path.exists(os.path.join(path, "HANDOFF.md")) else ""
    record("handoff", t, q, reqs_of(path, q["session_id"]), t_reqs, {"handoff_chars": len(hand)})
    return out


def main():
    global MODEL, WORK, QUIZ, CHECKS, setup
    work = os.path.abspath(sys.argv[1])
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    MODEL = sys.argv[3] if len(sys.argv) > 3 else MODEL
    scenario = sys.argv[4] if len(sys.argv) > 4 else "ledgr"
    if scenario == "relay":
        import scenario_relay
        WORK, QUIZ, CHECKS, setup = scenario_relay.WORK, scenario_relay.QUIZ, scenario_relay.CHECKS, scenario_relay.setup
    elif scenario != "ledgr":
        sys.exit(f"unknown scenario {scenario!r}")
    os.makedirs(work, exist_ok=True)
    res_path = os.path.join(work, "results.json")
    results = json.load(open(res_path)) if os.path.exists(res_path) else {}
    for rep in range(1, reps + 1):
        if f"rep{rep}" in results:
            prev = results[f"rep{rep}"]
            if (prev.get("model", "haiku"), prev.get("scenario", "ledgr")) != (MODEL, scenario):
                sys.exit(f"{res_path} has rep{rep} from {prev.get('model', 'haiku')}/{prev.get('scenario', 'ledgr')}; "
                         f"use a new workdir")
            continue
        t0 = time.time()
        results[f"rep{rep}"] = one_rep(work, rep)
        results[f"rep{rep}"]["secs"] = round(time.time() - t0)
        results[f"rep{rep}"]["model"] = MODEL
        results[f"rep{rep}"]["scenario"] = scenario
        with open(res_path, "w") as f:
            json.dump(results, f, indent=1)
    print(res_path)


if __name__ == "__main__":
    main()
