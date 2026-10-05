"""Large-project stress run: how ctxlc behaves when a session is huge.

Builds a synthetic multi-day transcript (default ~3,000 user turns, ~45 MB, 400 requirements across 400 modules)
and measures:
  - first full ingest time and per-hook latency on the big transcript (UserPromptSubmit, PostToolUse, SessionStart)
  - state.json size
  - digest size, and whether requirements relevant to the CURRENT work survive the budget
  - PreCompact instruction size

usage: python3 bench/scale.py [turns] [requirements]
"""
import io
import json
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlc import config, hooks, tokens  # noqa: E402
from ctxlc.store import Store  # noqa: E402

MODULES = [f"{a}{b}" for a in ("billing", "auth", "search", "ledger", "export", "sync", "audit", "quota", "mailer",
                               "upload", "geo", "pricing", "fraud", "report", "invoice", "tenant", "webhook", "cache",
                               "session", "notify") for b in ("", "-api", "-worker", "-admin", "-cli", "-gateway",
                                                              "-ingest", "-scheduler", "-store", "-ui", "-v2", "-edge",
                                                              "-batch", "-stream", "-index", "-proxy", "-sdk", "-jobs",
                                                              "-core", "-bridge")]


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def build(path, turns, n_req, seed=7):
    rnd = random.Random(seed)
    t0 = time.time() - turns * 60
    req_at = {int(i * turns / n_req): i for i in range(n_req)}
    with open(path, "w") as f:
        def w(r):
            f.write(json.dumps(r) + "\n")
        for k in range(turns):
            ts = t0 + k * 60
            if k in req_at:
                m = MODULES[req_at[k] % len(MODULES)]
                text = f"for {m}, responses must include the request id header x-req-{req_at[k]} and never exceed 2 seconds."
            else:
                m = rnd.choice(MODULES)
                text = f"look at {m} and continue with the refactor of src/{m}/handler.ts step {k}."
            w({"type": "user", "timestamp": iso(ts), "uuid": f"u{k}", "message": {"role": "user", "content": text}})
            tid = f"tool{k}"
            w({"type": "assistant", "timestamp": iso(ts + 5), "uuid": f"a{k}", "apiBlockIndex": 0,
               "message": {"model": "claude-opus-5-5", "content": [
                   {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"npm test -- src/{m}"}}],
                   "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1000,
                             "cache_read_input_tokens": 120000, "output_tokens": 300,
                             "cache_creation": {"ephemeral_1h_input_tokens": 1000}}}})
            body = "\n".join(f"  ok {k}.{j} src/{m}/case_{j}.spec.ts ({rnd.randint(1, 90)} ms)" for j in range(rnd.randint(60, 260)))
            w({"type": "user", "timestamp": iso(ts + 20), "uuid": f"r{k}", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": body + "\nTests: all passed"}]}})
            w({"type": "assistant", "timestamp": iso(ts + 30), "uuid": f"b{k}", "apiBlockIndex": 0,
               "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": f"Step {k} done for {m}."}],
                           "usage": {"input_tokens": 2, "cache_creation_input_tokens": 500,
                                     "cache_read_input_tokens": 121000, "output_tokens": 20,
                                     "cache_creation": {"ephemeral_1h_input_tokens": 500}}}})


def run(store_proj, payload):
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    t = time.perf_counter()
    try:
        hooks.main(io.StringIO(json.dumps(payload)))
    finally:
        sys.stdout = real
    return time.perf_counter() - t, out.getvalue()


def main(turns=3000, n_req=400):
    tmp = tempfile.mkdtemp(prefix="ctxlc-scale-")
    os.environ["CTXLC_PROJECT"] = tmp
    tr = os.path.join(tmp, "t.jsonl")
    build(tr, turns, n_req)
    mb = os.path.getsize(tr) / 1e6
    base = {"session_id": "s1", "transcript_path": tr, "cwd": tmp}
    res = {"turns": turns, "requirements": n_req, "transcript_mb": round(mb, 1)}

    res["first_ingest_s"], _ = run(tmp, {**base, "hook_event_name": "Stop"})
    res["stop_hook_incremental_s"], _ = run(tmp, {**base, "hook_event_name": "Stop"})
    res["prompt_hook_s"], _ = run(tmp, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "next"})
    big = "\n".join(f"line {i} " + "x" * 60 for i in range(3000))
    res["post_tool_hook_s"], _ = run(tmp, {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                           "tool_use_id": "z", "tool_input": {"command": "npm run build"},
                                           "tool_response": {"stdout": big, "stderr": "", "interrupted": False}})
    res["state_kb"] = round(os.path.getsize(os.path.join(Store(tmp).dir, "state.json")) / 1e3, 1)

    # Current work: the last prompt names one module whose requirement was stated long ago.
    target = 3  # requirement #3 → MODULES[3], stated in the first 1% of the session
    m = MODULES[target]
    with open(tr, "a") as f:
        f.write(json.dumps({"type": "user", "timestamp": iso(time.time()), "uuid": "ulast", "message": {
            "role": "user", "content": f"now fix the timeout bug in {m}, the p99 is way over budget."}}) + "\n")
    res["compact_start_s"], out = run(tmp, {**base, "hook_event_name": "SessionStart", "source": "compact"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    res["digest_tokens"] = tokens.estimate(ctx)
    res["injected_chars"] = len(ctx)  # must stay under Claude Code's 10,000-char hook cap
    res["digest_requirements_shown"] = ctx.count("x-req-")
    res["relevant_old_requirement_kept"] = f"x-req-{target} " in ctx
    res["digest_budget"] = config.load(Store(tmp).dir)["digest_budget_tokens"]
    _, pre = run(tmp, {**base, "hook_event_name": "PreCompact", "trigger": "auto"})
    res["pre_compact_tokens"] = tokens.estimate(pre)
    res["pre_compact_relevant_kept"] = f"x-req-{target} " in pre
    print(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:3]]
    main(*a)
