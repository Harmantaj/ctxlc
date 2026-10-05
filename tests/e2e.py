"""End-to-end check of ctxlc through its installed command line, on a throwaway project.

Usage: python3 tests/e2e.py [transcript.jsonl]  (a Claude Code transcript that opens with a long brief, so the
digest has a "Defining brief" section; it is copied, never modified. Default: the synthetic
tests/fixtures/brief_session.jsonl, so CI needs no real history).
"""
import json, os, shutil, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTX = [sys.executable, os.path.join(ROOT, "bin/ctx")]
REAL_T = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests/fixtures/brief_session.jsonl")
proj = tempfile.mkdtemp(prefix="ctxlc-e2e-")
env = dict(os.environ, CTXLC_PROJECT=proj)
t = os.path.join(proj, "t.jsonl")
shutil.copy(REAL_T, t)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def cli(*args, stdin=None):
    p = subprocess.run(CTX + list(args), input=stdin, capture_output=True, text=True, env=env, cwd=proj)
    return p.returncode, p.stdout, p.stderr


def hook(event, **kw):
    d = dict(session_id="e2e", transcript_path=t, cwd=proj, hook_event_name=event, **kw)
    t0 = time.time()
    p = subprocess.run(CTX + ["hook"], input=json.dumps(d), capture_output=True, text=True, env=env, cwd=proj)
    ms = (time.time() - t0) * 1000
    if event == "PreCompact":  # plain text: Claude Code appends it to the compaction instructions
        return p.returncode, p.stdout, ms, p.stderr
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None), ms, p.stderr


# --- hooks ---------------------------------------------------------------------------
rc, out, ms, err = hook("SessionStart", source="startup")
ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
check("SessionStart(startup) ingests the real transcript and injects a digest", rc == 0 and "Defining brief" in ctx and "Context protocol" in ctx,
      f"{len(ctx)} chars ≈ {len(ctx)//4} tokens, {ms:.0f} ms, transcript {os.path.getsize(t)/1e6:.1f} MB")

rc, o, _ = cli("handoff", "-", stdin="In progress: E2E\nVerified: hooks\nNext: report\n")
rc, out, ms, _ = hook("SessionStart", source="clear")
ctx = out["hookSpecificOutput"]["additionalContext"]
check("SessionStart(clear) puts the handoff first, one line per bullet",
      "- In progress: E2E\n- Verified: hooks\n- Next: report" in ctx and ctx.find("Handoff") < ctx.find("Defining brief"), f"{len(ctx)//4} tokens")
rc, out, _, _ = hook("SessionStart", source="compact")
check("SessionStart(compact) re-injects state", out and "Handoff" in out["hookSpecificOutput"]["additionalContext"])
rc, out, _, _ = hook("SessionStart", source="resume")
check("SessionStart(resume) injects nothing (history already present)", rc == 0 and out is None)

rc, out, _, _ = hook("PreToolUse", tool_name="Bash", tool_input={"command": "npm test"})
check("PreToolUse wraps a test command, no permission override",
      out and "updatedInput" in out["hookSpecificOutput"] and "permissionDecision" not in out["hookSpecificOutput"])
rc, out, _, _ = hook("PreToolUse", tool_name="Bash", tool_input={"command": "git status"})
check("PreToolUse leaves ordinary commands alone", rc == 0 and out is None)

big = "\n".join(f"line {i}: ok" for i in range(3000)) + "\nERROR: build failed at step 7\n" + "\n".join(f"tail {i}" for i in range(50))
rc, out, ms, _ = hook("PostToolUse", tool_name="Bash", tool_input={"command": "make"},
                      tool_response={"stdout": big, "stderr": "", "interrupted": False, "isImage": False})
new = (out or {}).get("hookSpecificOutput", {}).get("updatedToolOutput") or {}
s = json.dumps(new)
check("PostToolUse condenses big output, keeps the error, keeps the output shape",
      "ERROR: build failed at step 7" in s and len(s) < len(big) / 3 and "stdout" in new, f"{len(big):,} → {len(s):,} chars, {ms:.0f} ms")
rc, out, _, _ = hook("PostToolUse", tool_name="Bash", tool_input={"command": "make"},
                     tool_response={"stdout": big, "stderr": "", "interrupted": False, "isImage": False})
s2 = json.dumps((out or {}).get("hookSpecificOutput", {}).get("updatedToolOutput") or {})
check("PostToolUse turns an identical repeat into a pointer", 0 < len(s2) < len(s), f"{len(s2)} chars")
rc, out, _, _ = hook("PostToolUse", tool_name="Bash", tool_input={"command": f'"{ROOT}/bin/ctx" digest'},
                     tool_response={"stdout": big, "stderr": "", "interrupted": False, "isImage": False})
check("PostToolUse never condenses ctx's own output", rc == 0 and out is None)

for ev in ("Stop", "PreCompact", "SessionEnd"):
    rc, out, ms, err = hook(ev)
    check(f"{ev} ingests incrementally", rc == 0 and not err, f"{ms:.0f} ms")
    if ev == "PreCompact":
        check("PreCompact tells the summary which state ctxlc restores", "refer to these by ID" in out and "- R1 " in out)
rc, out, _, _ = hook("PostCompact", trigger="auto", compact_summary="Summary: E2E compaction marker ZQX")
_, o, _ = cli("search", "ZQX")
check("PostCompact archives the summary and it is searchable", "ZQX" in o)

rc, out, _, _ = hook("PreModelSwitch", from_model="a", to_model="b", context_tokens=250000, prompt_cache_warm=True,
                     estimated_cache_write_usd=4.1, source="command", cache_ttl="1h")
check("PreModelSwitch asks before discarding a warm 250k cache", out and out["hookSpecificOutput"]["permissionDecision"] == "ask")
rc, out, _, _ = hook("PreModelSwitch", from_model="a", to_model="b", context_tokens=20000, prompt_cache_warm=True, source="command")
check("PreModelSwitch stays quiet for a small context", rc == 0 and out is None)
rc, out, _, _ = hook("PostModelSwitch", from_model="a", to_model="b", context_tokens=20000, source="command")
check("PostModelSwitch records a metric", rc == 0)

# Idle guard: append a 10-hour-old, 150k-token assistant turn.
old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 36000))
with open(t, "a") as f:
    f.write(json.dumps({"type": "assistant", "timestamp": old, "message": {"model": "claude-x", "role": "assistant",
            "content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 10, "cache_read_input_tokens": 150000,
            "cache_creation_input_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 5}}}}) + "\n")
rc, out, _, _ = hook("UserPromptSubmit", prompt="continue")
check("UserPromptSubmit holds the first prompt after a long idle on a big context", out and out.get("decision") == "block",
      (out or {}).get("reason", "")[:70] + "…")
rc, out, _, _ = hook("UserPromptSubmit", prompt="continue")
check("UserPromptSubmit lets the resent prompt through", rc == 0 and out is None)
check("no hook errors logged", not os.path.exists(os.path.join(proj, ".claude/context/errors.log")))

# --- CLI -------------------------------------------------------------------------------
rc, o, _ = cli("note", "decision", "Use Postgres, not SQLite")
did = o.strip()
rc2, o2, _ = cli("note", "decision", "Use MySQL instead", "--supersedes", did)
_, st, _ = cli("state", "--all")
check("note decision + --supersedes", did and "superseded" in st.lower() and "MySQL" in st, did)
rc, o, _ = cli("note", "bug", "login times out")
bid = o.strip()
_, d1, _ = cli("digest"); cli("resolve", bid); _, d2, _ = cli("digest")
check("note bug → digest → resolve removes it", "login times out" in d1 and "login times out" not in d2, bid)
cli("task", "Finish E2E"); _, d, _ = cli("digest")
check("task appears in digest", "Finish E2E" in d)
rc, o, _ = cli("search", "context optimization model switch")
check("search finds real history", rc == 0 and o.count("\n") >= 3)
import re; m = re.search(r"^\[(h\d+)\]", o, re.M); ref = m and m.group(1)
rc, o, _ = cli("show", ref) if ref else (1, "", "")
check("show retrieves a search hit", rc == 0 and len(o) > 20, ref or "no ref")
rc, o, _ = cli("status", "--json"); s = json.loads(o)
check("status --json", rc == 0 and s["digest_tokens"] > 0, f"digest {s['digest_tokens']} tokens")
usage = json.dumps({"context": {"tokensUsed": 150000, "contextWindow": 200000, "percentUsed": 75, "autoCompactsAtPercent": 80},
                    "plan": {"windows": [{"label": "Weekly", "percentUsed": 96, "resetsIn": "2d"}]}})
rc, o, _ = cli("panel", "--usage", "-", stdin=usage)
# A cold cache (transcript older than the TTL) outranks the near-compaction warning.
want = "75% full" if s["cache_warm"] else "Cache is cold"
check("panel renders with live usage and the right warning", rc == 0 and want in o and 'data-a="reset"' in o and "Weekly 96%" in o, want)
rc, o, _ = cli("panel", "--usage", "-", stdin="not json")
check("panel survives bad usage JSON", rc == 0 and "Smart reset" in o)
rc, o, _ = cli("report")
check("report runs", rc == 0 and len(o) > 50)

shutil.rmtree(proj)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
