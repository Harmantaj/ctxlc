import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlc import config, history, hooks, ingest, retrieve, toolout  # noqa: E402
from ctxlc.store import Store, add_item, empty_state, render_digest  # noqa: E402


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def rec_user(text, ts, **kw):
    return dict(type="user", timestamp=iso(ts), uuid=f"u{ts}", message={"role": "user", "content": text}, **kw)


def rec_asst(blocks, ts, ctx=50000, model="claude-opus-5-5"):
    return {
        "type": "assistant", "timestamp": iso(ts), "uuid": f"a{ts}", "apiBlockIndex": 0,
        "message": {"model": model, "content": blocks, "usage": {
            "input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": ctx - 1002,
            "output_tokens": 50, "cache_creation": {"ephemeral_1h_input_tokens": 1000}}},
    }


def rec_result(tool_id, text, ts, is_error=False):
    return {"type": "user", "timestamp": iso(ts), "uuid": f"r{ts}", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = self.tmp.name
        os.environ["CTXLC_PROJECT"] = self.proj
        self.store = Store(self.proj)
        self.cfg = config.load(self.store.dir)
        self.transcript = os.path.join(self.proj, "t.jsonl")

    def tearDown(self):
        os.environ.pop("CTXLC_PROJECT", None)
        self.tmp.cleanup()

    def write(self, *recs):
        with open(self.transcript, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def run_hook(self, payload):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            hooks.main(io.StringIO(json.dumps(payload)))
        finally:
            sys.stdout = real
        err = os.path.join(self.store.dir, "errors.log")
        if os.path.exists(err):
            self.fail(open(err).read())
        return json.loads(out.getvalue()) if out.getvalue() else None


class TestItems(Base):
    def test_explicit_and_auto_supersede(self):
        st = empty_state(self.proj)
        d1, _ = add_item(st, "decision", "Use library requests for HTTP calls")
        d2, rep = add_item(st, "decision", "Switched to httpx instead of requests", supersedes=[d1["id"]])
        self.assertEqual(rep, [d1["id"]])
        self.assertEqual(d1["status"], "superseded")
        # near-duplicate wording supersedes automatically
        c1, _ = add_item(st, "constraint", "API timeout must be 30 seconds for the upstream client")
        c2, rep = add_item(st, "constraint", "API timeout must be 10 seconds for the upstream client")
        self.assertEqual(rep, [c1["id"]])
        # unrelated items stay active
        c3, rep = add_item(st, "constraint", "Never log user passwords")
        self.assertEqual(rep, [])
        self.assertEqual(c2["status"], "active")

    def test_templated_rules_for_different_subjects_stay_active(self):
        # Large projects state the same rule shape per service; similar wording about two services is two rules.
        st = empty_state(self.proj)
        a, _ = add_item(st, "requirement", "for billing-api, responses must include the request id header x-req-1")
        b, rep = add_item(st, "requirement", "for auth-api, responses must include the request id header x-req-2")
        self.assertEqual(rep, [])
        self.assertEqual(a["status"], "active")
        # a refinement of the same rule (only adds words) still replaces it
        c, rep = add_item(st, "requirement", "for auth-api, responses must always include the request id header x-req-2 and a trace id")
        self.assertEqual(rep, [b["id"]])

    def test_digest_keeps_old_rule_relevant_to_current_work(self):
        st = empty_state(self.proj)
        t = time.time() - 10_000
        for i in range(300):
            add_item(st, "requirement", f"for svc{i}, every response must carry header x-req-{i} within 2 seconds", ts=t + i)
        st["prompts"].append({"ts": t + 400, "session": "s", "text": "now fix the timeout bug in svc3", "uuid": "p"})
        out = render_digest(st, budget_tokens=1500)
        self.assertIn("x-req-3 ", out)
        self.assertIn("more (`ctx state`)", out)
        self.assertNotIn("x-req-150 ", out)

    def test_single_current_task(self):
        st = empty_state(self.proj)
        t1, _ = add_item(st, "task", "Implement login")
        t2, rep = add_item(st, "task", "Write tests for billing")
        self.assertEqual(rep, [t1["id"]])
        self.assertEqual(t1["status"], "done")

    def test_digest_budget_keeps_requirements_first(self):
        st = empty_state(self.proj)
        add_item(st, "requirement", "Must support Python 3.9")
        add_item(st, "task", "Ship v1")
        for i in range(200):
            st["prompts"].append({"ts": time.time(), "text": f"filler prompt number {i} " * 20})
        st["files"] = {f"/p/f{i}.py": {"edits": 1, "last_edit": time.time()} for i in range(50)}
        out = render_digest(st, budget_tokens=400, recent_prompts=6)
        self.assertIn("Must support Python 3.9", out)
        self.assertIn("Ship v1", out)
        self.assertLess(len(out) / 3.6, 470)
        superseded_hidden = render_digest(st, 5000)
        self.assertIn("superseded item(s) hidden", superseded_hidden)

    def test_handoff_keeps_its_lines(self):
        st = empty_state(self.proj)
        st["handoff"] = {"ts": time.time(), "model": "m", "text": "In progress: X\n- Verified: Y\n\nNext: Z"}
        out = render_digest(st)
        self.assertIn("- In progress: X\n- Verified: Y\n- Next: Z", out)
        st["handoff"]["text"] = "\n".join(["x" * 300] * 20)
        out = render_digest(st, 5000)
        self.assertLessEqual(out.count("x" * 300), 7)

    def test_handoff_is_for_the_next_session_only(self):
        from ctxlc.store import handoff_live
        ho = {"ts": time.time(), "text": "Next: Z"}
        self.assertTrue(handoff_live(ho, "s2"))
        ho.update(consumed_by="s2", consumed_at=time.time() - 7 * 3600)
        self.assertTrue(handoff_live(ho, "s2"))  # the consumer keeps it across its compactions
        self.assertFalse(handoff_live(ho, "s3"))
        ho["consumed_at"] = time.time() - 600
        self.assertTrue(handoff_live(ho, "s3"))  # a quick second restart still gets it


class TestIngest(Base):
    def test_incremental_extraction(self):
        t0 = time.time() - 100
        self.write(
            rec_user("<system-reminder>ignore me</system-reminder>Build a CLI. It must never write outside the project dir.", t0),
            rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "pytest -q"}},
                      {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "/p/app.py"}},
                      {"type": "tool_use", "id": "td", "name": "TodoWrite", "input": {"todos": [{"content": "add flag", "status": "in_progress"}]}}], t0 + 1),
            rec_result("b1", "E   AssertionError: expected 3\n1 failed", t0 + 2, is_error=True),
            rec_user("<command-name>/model</command-name>", t0 + 3),
            rec_user("x", t0 + 3, isMeta=True),
        )
        st = empty_state(self.proj)
        n1 = ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual(n1, 5)
        self.assertEqual(len(st["prompts"]), 1)
        self.assertNotIn("ignore me", st["prompts"][0]["text"])
        self.assertTrue(any("never write outside" in i["text"] for i in st["items"]))
        self.assertEqual(st["files"]["/p/app.py"]["edits"], 1)
        self.assertEqual(st["todos"]["items"][0]["content"], "add flag")
        self.assertFalse(st["failures"][0]["resolved"])
        # second pass: nothing new
        self.assertEqual(ingest.ingest_transcript(self.store, st, self.transcript), 0)
        # the same command later succeeds -> failure resolved; partial trailing line is not consumed
        self.write(rec_asst([{"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "pytest -q"}},
                             {"type": "text", "text": "All tests pass now."}], t0 + 5),
                   rec_result("b2", "3 passed", t0 + 6))
        with open(self.transcript, "a") as f:
            f.write('{"type": "user", "partial')
        self.assertEqual(ingest.ingest_transcript(self.store, st, self.transcript), 2)
        self.assertTrue(st["failures"][0]["resolved"])
        self.assertEqual(st["last_status"]["text"], "All tests pass now.")


class TestDirectives(Base):
    def test_hard_wrapped_and_descriptive(self):
        text = ("It has never made money, and has never found an edge.\n"
                "Do not retune or backtest on run `4430b7dd` because the\nresult is overfit.\n\n"
                "- Always use `venv/bin/python`.\n- The API must stay read-only for testers.")
        got = ingest.directives(text)
        self.assertIn("Do not retune or backtest on run 4430b7dd because the result is overfit.", got)
        self.assertIn("Always use venv/bin/python.", got)
        self.assertIn("The API must stay read-only for testers.", got)
        self.assertFalse(any(g.startswith("It has never") for g in got))

    def test_user_decisions_supersede(self):
        t0 = time.time()
        self.write(rec_user("The CLI must be named ledgr. For now we'll use SQLite for storage.", t0),
                   rec_user("Decision change: we're switching storage from SQLite to DuckDB instead.", t0 + 1))
        with self.store.transaction() as st:
            ingest.ingest_transcript(self.store, st, self.transcript)
            d = render_digest(st, 2500)
        items = {i["id"]: i for i in self.store.load()["items"]}
        self.assertEqual(items["D1"]["status"], "superseded")
        self.assertEqual(items["D2"]["supersedes"], ["D1"])
        self.assertIn("replaces D1", d)
        self.assertEqual(items["R1"]["status"], "active")

    def test_instead_of_needs_someone_choosing(self):
        kinds = lambda t: [k for k, _ in ingest.classify(t)]
        self.assertEqual(kinds("Teams send requests to the gateway instead of calling providers directly."), [])
        self.assertEqual(kinds("We'll keep REST instead of gRPC for now."), ["decision"])
        self.assertEqual(kinds("Use httpx instead of requests."), ["decision"])

    def test_change_replaces_only_what_it_names(self):
        t0 = time.time()
        self.write(rec_user("For now we'll use SQLite for the lumen cache. We'll use Redis for lumen sessions.", t0),
                   rec_user("Decision change: we're switching the lumen cache from SQLite to Postgres.", t0 + 1))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual([(i["id"], i["status"]) for i in st["items"]],
                         [("D1", "superseded"), ("D2", "active"), ("D3", "active")])  # sharing "lumen" is not enough

    def test_coordinated_description_is_not_a_rule(self):
        self.assertEqual(ingest.directives("The digest is always injected, and never expires after an hour."), [])
        self.assertEqual(ingest.directives("The hook always runs first, but never blocks the prompt."), [])
        self.assertTrue(ingest.directives("Ship the build, and never embed secrets in it."))
        self.assertTrue(ingest.directives("Integer-only currency representation (cents, never floats)"))

    def test_questions_are_not_directives(self):
        self.assertEqual(ingest.directives("How must money amounts be stored? What should the CLI be named?"), [])

    def test_all_failures_captured(self):
        text = ("[ctxlc] 22,000 chars condensed. COMPLETE for errors: every line matching error/warning/failure patterns\n"
                "178: tests/test_io.py::test_import_unicode_names FAILED\n179:   UnicodeDecodeError: 'ascii' codec\n"
                "405: tests/test_split.py::test_split_bill_rounding FAILED\n406:   AssertionError: lost 1 cent\n[ctxlc] exit code 2")
        got = ingest.failure_lines(text)
        self.assertIn("test_split_bill_rounding", got)
        self.assertIn("lost 1 cent", got)
        self.assertNotIn("COMPLETE", got)

    def test_hedged_and_lead_in_sentences_are_not_directives(self):
        text = ("A large amount of context may need to be sent again.\n\n"
                "Work when switching models It must also handle:\n\n"
                "The system must work regardless of the model.")
        self.assertEqual(ingest.directives(text), ["The system must work regardless of the model."])

    def test_lookup_commands_are_not_failures(self):
        from ctxlc.ingest import is_probe
        self.assertTrue(is_probe("ls ~/.claude/skills 2>&1 | head; ls ~/.claude/skills/ctx 2>&1"))
        self.assertTrue(is_probe('cd "/x y" && grep -n foo bar.py | head'))
        self.assertFalse(is_probe("npm test"))
        self.assertFalse(is_probe("cd app && make build | tail -5"))

    def test_failures_have_ids_and_can_be_resolved(self):
        from ctxlc.store import set_status
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "make"}}], time.time()),
                   rec_result("b1", "money.c:88: error: loses precision", time.time(), True))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual(st["failures"][0]["id"], "F1")
        self.assertIn("[F1]", render_digest(st))
        self.assertTrue(set_status(st, "f1", "resolved"))
        self.assertNotIn("[F1]", render_digest(st))
        st["failures"].append({"ts": time.time(), "cmd": "old", "err": "e", "resolved": False})  # pre-ID record
        self.assertIn("[F2]", render_digest(st))

    def test_broader_success_resolves_failure(self):
        t = time.time()
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "cd app && npm test -- billing.spec"}}], t),
                   rec_result("b1", "FAIL billing.spec: expected 3", t, True),
                   rec_asst([{"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "python3 -V"}}], t + 1),
                   rec_result("b2", "Python 3.14", t + 1))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertFalse(st["failures"][0]["resolved"])  # an unrelated success is not a fix
        self.write(rec_asst([{"type": "tool_use", "id": "b3", "name": "Bash", "input": {"command": "npm test"}}], t + 2),
                   rec_result("b3", "all passed", t + 2))
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertTrue(st["failures"][0]["resolved"])

    def test_identifiers_keep_underscores(self):
        self.assertEqual(ingest.directives("The CSV columns must be `date,payer,amount_cents,memo` in _exactly_ that order."),
                         ["The CSV columns must be date,payer,amount_cents,memo in exactly that order."])

    def test_casual_preferences_are_requirements(self):
        got = ingest.directives("Oh, and personally I can't stand pandas — keep it out of this project, it's way too heavy for a CLI. "
                                "I'd rather keep the output plain. The report looks fine, and the tests pass.")
        self.assertEqual(got, ["Oh, and personally I can't stand pandas — keep it out of this project, it's way too heavy for a CLI.",
                               "I'd rather keep the output plain."])
        self.assertEqual(ingest.directives("It ran fine, and keep going was what I said. I can't stand here all day? "
                                           'The test case is "I hate pandas — keep it out of this project."'), [])

    def _test_failure(self, t):
        out = ("tests/test_io.py::test_import_unicode_names FAILED\n  UnicodeDecodeError: 'ascii' codec can't decode byte 0xc3\n"
               "tests/test_split.py::test_split_bill_rounding FAILED\n  AssertionError: split_bill(100, 3) returned [33, 33, 33] (lost 1 cent)\n"
               "=========== 2 failed, 518 passed in 12.34s ===========")
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "make test"}}], t),
                   rec_result("b1", out, t, True))

    def test_user_reported_fix_closes_only_that_part(self):
        t = time.time()
        self._test_failure(t)
        self.write(rec_user("Is the unicode bug fixed? It's not fixed on CI. The rounding bug is still not resolved.", t + 1))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertNotIn("fixed_lines", st["failures"][0])  # questions and negations claim nothing
        self.write(rec_user("I just patched the unicode import bug myself on my machine, so that failure is fixed — "
                            "only the rounding failure is still open. Next, add CSV export.", t + 2))
        ingest.ingest_transcript(self.store, st, self.transcript)
        f = st["failures"][0]
        self.assertFalse(f["resolved"])
        self.assertNotIn("unicode", f["err"].lower())
        self.assertIn("test_split_bill_rounding", f["err"])
        d = render_digest(st)
        self.assertIn("test_split_bill_rounding", d)
        self.assertIn("user reported fixed since: tests/test_io.py::test_import_unicode_names", d)

    def test_user_reported_fix_resolves_whole_failure(self):
        t = time.time()
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "make"}}], t),
                   rec_result("b1", "money.c:88: error: implicit conversion loses precision", t, True),
                   rec_user("ok I fixed the money.c precision error locally", t + 1))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertTrue(st["failures"][0]["resolved"])
        self.assertNotIn("[F1]", render_digest(st))

    def test_piped_failures_fill_in_a_truncated_log(self):
        t = time.time()
        bash = lambda i, cmd, out, err=False: (rec_asst([{"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {"command": cmd}}], t + i),
                                               rec_result(f"b{i}", out, t + i, err))
        self.write(*bash(1, "make build", "Exit code 2\n[0000/4000] CC unit_0.c ... ok\n[0001/4000] CC unit_1.c", True),
                   *bash(2, "make build 2>&1 | grep -E 'error' | head", "src/money.c:88: error: loses precision"),
                   *bash(3, "cat build.log | grep error", "src/old.c:1: error: stale"),
                   *bash(4, "make lint | tail -1", "lint finished: 3 warnings, 0 errors"),
                   *bash(5, "cd app && make test 2>&1 | tail -3", "=== 1 failed, 9 passed ===\nmake: *** [test] Error 1"))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual([(f["cmd"], f["err"]) for f in st["failures"]],
                         [("make build", "src/money.c:88: error: loses precision"),
                          ("cd app && make test 2>&1 | tail -3", "=== 1 failed, 9 passed === ⏎ make: *** [test] Error 1")])
        self.write(*bash(6, "make test", "10 passed"))
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertTrue(st["failures"][1]["resolved"])

    def test_redirected_run_and_its_log_reads(self):
        t = time.time()
        bash = lambda i, cmd, out: (rec_asst([{"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {"command": cmd}}], t + i),
                                    rec_result(f"b{i}", out, t + i))
        self.write(*bash(1, './dev test > /tmp/x/out.log 2>&1; echo "exit=$?"; grep -c FAIL /tmp/x/out.log',
                         "exit=1\n2"),
                   *bash(2, "L=/tmp/x/out.log; sed -n '60,70p' $L",
                         "FAIL src/auth/token.spec.ts\n    Expected: 401\n    Received: 200"),
                   *bash(3, './dev build > b.log 2>&1; echo "exit=$?"', "exit=0"),
                   *bash(4, "./dev build 2>&1 | tail -2", "src/a.ts(1,2): error TS2322: Type 'string' is not assignable"),
                   # A backslash in a variable value must not be read as a regex replacement escape.
                   *bash(5, r"P=C:\logs\ ; cat $P/x", "nothing"))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual(len(st["failures"]), 2)
        self.assertIn("Received: 200", st["failures"][0]["err"])
        self.assertIn("TS2322", st["failures"][1]["err"])

    def test_relay_phrasings(self):
        t = time.time()
        self.write(rec_user("We'll queue deliveries in Redis.", t),
                   rec_user("Every endpoint requires an API key, except GET /healthz. Btw, no ORMs, ever — we write the SQL by hand.", t + 1),
                   rec_user("Compare exp against the monotonic clock with a 30-second leeway. Don't implement it yet.", t + 2),
                   rec_user("Change of plan on the queue: we're moving from Redis to NATS, because we need replay.", t + 3),
                   rec_user("Scratch NATS — the platform team only supports Kafka, so we're going with Kafka after all.", t + 4),
                   rec_user("The current task is per-tenant rate limiting: 120 requests per minute per API key.", t + 5))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        d = render_digest(st).lower()
        self.assertIn("except get /healthz", d)
        self.assertIn("no orms, ever", d)
        self.assertIn("## current task\n- [t1] the current task is per-tenant rate limiting", d)
        self.assertFalse([i for i in st["items"] if "implement it yet" in i["text"]])
        decisions = d.split("## decisions (active)")[1].split("\n\n")[0]
        self.assertIn("kafka", decisions)
        self.assertNotIn("in redis.", decisions)  # "Redis." with its full stop still names redis

    def test_third_person_and_green_now_fixes(self):
        t = time.time()
        out = ("FAIL src/ratelimit/limiter.spec.ts\nFAIL src/webhooks/signature.spec.ts\n"
               "Error: ENOENT: no such file or directory, open 'fixtures/webhook_payload.json'\nFAIL src/auth/token.spec.ts")
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "./dev test"}}], t),
                   rec_result("b1", out, t, True),
                   rec_user("Marco bumped the limiter's test timeout and that one is green now. The webhook signature failure "
                            "was just a missing fixture file; Marco fixed that too. The expired-token one is still broken.", t + 1))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        f = st["failures"][0]
        self.assertFalse(f["resolved"])
        self.assertEqual(f["err"], "FAIL src/auth/token.spec.ts")

    def test_control_prompts_are_not_conversation(self):
        t = time.time()
        self.write(rec_user("ctx panel", t), rec_user("/ctx reset", t + 1), rec_user("now fix the login bug", t + 2))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual([p["text"] for p in st["prompts"]], ["now fix the login bug"])
        self.assertTrue(any(r["text"] == "ctx panel" for r in self.store.iter_jsonl(self.store.history_path)))

    def test_permission_denials_are_not_failures(self):
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "rm x"}}], time.time()),
                   rec_result("b1", "Permission for this action was denied by the Claude Code auto mode classifier.", time.time(), True))
        st = empty_state(self.proj)
        ingest.ingest_transcript(self.store, st, self.transcript)
        self.assertEqual(st["failures"], [])

    def test_brief_kept_and_retrievable(self):
        long = "MISSION: build the ledger. " + "detail " * 400
        self.write(rec_user(long, time.time()))
        with self.store.transaction() as st:
            ingest.ingest_transcript(self.store, st, self.transcript)
            d = render_digest(st, 2500)
        self.assertIn("Defining brief", d)
        ref = d.split("show ")[-1].split("`")[0]
        self.assertTrue(retrieve.show(self.store, self.store.load(), ref).endswith("detail detail"))


class TestToolOutput(Base):
    def big_log(self):
        lines = [f"[{i:05d}] compiling module_{i}.c ok" for i in range(3000)]
        lines[1500] = "src/net.c:42: error: 'sock' undeclared"
        lines.append("BUILD FAILED: 1 error, 0 warnings")
        return "\n".join(lines)

    def test_compaction_keeps_signal_and_archives(self):
        ep = {"outputs": {}, "reads": {}}
        text = self.big_log()
        new, m = toolout.compact_bash(self.store, self.cfg, ep, {"command": "make"}, {"stdout": text, "stderr": ""})
        self.assertIsNotNone(new)
        out = new["stdout"]
        self.assertIn("'sock' undeclared", out)
        self.assertIn("BUILD FAILED", out)
        self.assertLess(len(out), 3200)
        self.assertIn("COMPLETE: every distinct line is shown", out)
        path = out.split("not covered: ")[1].split(" ")[0]
        self.assertIn("'sock' undeclared", open(path).read())
        self.assertGreater(m["raw_chars"] / m["new_chars"], 20)

    def test_persisted_output_uses_full_file(self):
        full = self.big_log()
        p = os.path.join(self.proj, "persisted.txt")
        with open(p, "w") as f:
            f.write(full)
        resp = {"stdout": full[:30000], "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False,
                "persistedOutputPath": p, "persistedOutputSize": len(full)}
        new, m = toolout.compact_bash(self.store, self.cfg, {"outputs": {}, "reads": {}}, {"command": "make"}, resp)
        self.assertIn("'sock' undeclared", new["stdout"])  # beyond the 30k head
        self.assertIn(p, new["stdout"])
        self.assertNotIn("persistedOutputPath", new)
        self.assertEqual(m["raw_chars"], len(full))

    def test_exit_mark_only_counts_as_a_line(self):
        from ctxlc.toolout import EXIT_LINE
        self.assertTrue(EXIT_LINE.search("FAIL: x\n[ctxlc] exit code 2"))
        self.assertFalse(EXIT_LINE.search('EXIT_MARK = "[ctxlc] exit code"\nmore'))

    def test_ctx_cli_output_is_not_condensed(self):
        from ctxlc.toolout import PASSTHROUGH
        self.assertTrue(PASSTHROUGH.search('cd "/a b" && bin/ctx digest'))
        self.assertTrue(PASSTHROUGH.search('"/a b/bin/ctx" search "x y"'))
        self.assertTrue(PASSTHROUGH.search("python3 -m ctxlc state --all"))
        self.assertFalse(PASSTHROUGH.search("npm run build"))
        self.assertFalse(PASSTHROUGH.search("ls bin/ctxlc-tools"))

    def test_dedup_and_passthrough(self):
        ep = {"outputs": {}, "reads": {}}
        small = "x" * 1000
        self.assertIsNone(toolout.compact_bash(self.store, self.cfg, ep, {"command": "cat a"}, {"stdout": small})[0])
        new, m = toolout.compact_bash(self.store, self.cfg, ep, {"command": "cat a"}, {"stdout": small})
        self.assertEqual(m["kind"], "bash_dedup")
        self.assertIsNone(toolout.compact_bash(self.store, self.cfg, {"outputs": {}, "reads": {}}, {"command": "make # ctx:full"}, {"stdout": self.big_log()})[0])

    def test_dedup_ignores_duplicate_hook_run_for_same_call(self):
        # Claude Code 2.1.286 runs one PostToolUse hook twice per call; the second run must not hide the output.
        ep = {"outputs": {}, "reads": {}}
        out = {"stdout": "x" * 1000}
        for _ in range(2):
            self.assertIsNone(toolout.compact_bash(self.store, self.cfg, ep, {"command": "cat a"}, out, call_id="t1")[0])
        self.assertEqual(toolout.compact_bash(self.store, self.cfg, ep, {"command": "cat a"}, out, call_id="t2")[1]["kind"], "bash_dedup")
        read = {"type": "text", "file": {"filePath": "/a.py", "content": "y" * 1000}}
        for _ in range(2):
            self.assertIsNone(toolout.dedup_read(self.cfg, ep, {"file_path": "/a.py"}, read, call_id="r1")[0])
        self.assertIsNotNone(toolout.dedup_read(self.cfg, ep, {"file_path": "/a.py"}, read, call_id="r2")[0])

    def test_file_viewers_pass_through(self):
        src = "\n".join(f"def f{i}(x):\n    return x + {i}" for i in range(400))  # ~10k chars of code
        for cmd in ("cat app.py", "sed -n 1,800p app.py", 'cd "/a b" && head -800 app.py', "git show HEAD:app.py",
                    "git diff", "grep -n def app.py | head -500", "nl -ba app.py"):
            ep = {"outputs": {}, "reads": {}}
            self.assertIsNone(toolout.compact_bash(self.store, self.cfg, ep, {"command": cmd}, {"stdout": src})[0], cmd)
        for cmd in ("make", "cat app.py && make", "python3 app.py", "./run | head"):
            ep = {"outputs": {}, "reads": {}}
            self.assertIsNotNone(toolout.compact_bash(self.store, self.cfg, ep, {"command": cmd}, {"stdout": src})[0], cmd)
        huge = src * 4  # above viewer_passthrough_max_chars: Claude Code would truncate it anyway
        self.assertIsNotNone(toolout.compact_bash(self.store, self.cfg, {"outputs": {}, "reads": {}}, {"command": "cat app.py"}, {"stdout": huge})[0])

    def test_wrap_only_noisy_commands(self):
        for cmd in ("make test", "cd app && npm test", "FOO=1 pytest -q tests/", "python3 -m pytest", "ls && cargo build"):
            self.assertIsNotNone(toolout.wrap_command({"command": cmd}), cmd)
        for cmd in ("ls -la", "git status", "cat Makefile", "echo make it", "make test # ctx:full"):
            self.assertIsNone(toolout.wrap_command({"command": cmd}), cmd)
        self.assertIsNone(toolout.wrap_command({"command": "npm test", "run_in_background": True}))
        once = toolout.wrap_command({"command": "make"})
        self.assertIsNone(toolout.wrap_command(once))
        import subprocess
        out = subprocess.run(["bash", "-c", toolout.wrap_command({"command": "make -f /nonexistent 2>/dev/null"})["command"]], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("[ctxlc] exit code 2", out.stdout)
        ok = subprocess.run(["bash", "-c", toolout.wrap_command({"command": "make -v >/dev/null"})["command"]], capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout), (0, ""))

    def test_diff_summary(self):
        diff = "\n".join(["diff --git a/x.py b/x.py", "--- a/x.py", "+++ b/x.py", "@@ -1,3 +1,4 @@ def f():"] + ["+new line"] * 3000)
        new, _ = toolout.compact_bash(self.store, self.cfg, {"outputs": {}, "reads": {}}, {"command": "git diff"}, {"stdout": diff})
        self.assertIn("x.py  +3000 -0", new["stdout"])

    def test_read_dedup(self):
        ep = {"outputs": {}, "reads": {}}
        resp = {"type": "text", "file": {"filePath": "/p/a.py", "content": "y" * 2000, "numLines": 1, "startLine": 1, "totalLines": 1}}
        self.assertIsNone(toolout.dedup_read(self.cfg, ep, {"file_path": "/p/a.py"}, resp)[0])
        new, m = toolout.dedup_read(self.cfg, ep, {"file_path": "/p/a.py"}, resp)
        self.assertIn("unchanged", new["file"]["content"])
        changed = json.loads(json.dumps(resp))
        changed["file"]["content"] = "z" * 2000
        self.assertIsNone(toolout.dedup_read(self.cfg, ep, {"file_path": "/p/a.py"}, changed)[0])


class TestRetrieve(Base):
    def test_finds_superseded_and_history(self):
        with self.store.transaction() as st:
            d1, _ = add_item(st, "decision", "Use PostgreSQL for the ledger")
            add_item(st, "decision", "Moved ledger to SQLite for local-first", supersedes=[d1["id"]])
        self.store.history({"ts": time.time(), "kind": "prompt", "text": "the JWT validation bug is in auth/login.ts"})
        st = self.store.load()
        hits = retrieve.search(self.store, st, "postgresql ledger")
        self.assertEqual(hits[0]["ref"], "D1")
        self.assertIn("SUPERSEDED", hits[0]["label"])
        hits = retrieve.search(self.store, st, "jwt validation")
        self.assertEqual(hits[0]["label"], "prompt")
        self.assertIn("login.ts", retrieve.show(self.store, st, hits[0]["ref"]))


class TestHooks(Base):
    def base(self, event, **kw):
        return dict(session_id="s1", transcript_path=self.transcript, cwd=self.proj, hook_event_name=event, **kw)

    def test_plugin_copy_stands_down_beside_installed_hooks(self):
        # claude.ai plugins load into local sessions too; with `ctx install` hooks there, state was injected twice.
        cfg = tempfile.TemporaryDirectory()
        self.addCleanup(cfg.cleanup)
        os.environ["CLAUDE_CONFIG_DIR"], os.environ["CTXLC_PLUGIN"] = cfg.name, "1"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("CLAUDE_CONFIG_DIR", "CTXLC_PLUGIN")])
        self.assertIsNotNone(self.run_hook(self.base("SessionStart", source="startup")))  # Cowork: no install
        with open(os.path.join(cfg.name, "settings.json"), "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": 'python3 "/x/bin/ctx" hook'}]}]}}, f)
        self.assertIsNone(self.run_hook(self.base("SessionStart", source="startup")))
        os.environ.pop("CTXLC_PLUGIN")
        self.assertIsNotNone(self.run_hook(self.base("SessionStart", source="startup")))  # the installed copy runs

    def test_cowork_session_keeps_store_out_of_outputs_and_injects_no_ctx_commands(self):
        session = os.path.join(self.proj, "local-agent-mode-sessions", "acct", "org", "local_abc")
        outputs = os.path.join(session, "outputs")
        os.makedirs(outputs)
        os.environ.pop("CTXLC_PROJECT")
        self.write(rec_user("the report must be a pdf, never a docx.", time.time() - 50))
        out = self.run_hook(dict(self.base("SessionStart", source="compact"), cwd=outputs))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("never a docx", ctx)
        self.assertNotIn(config.CTX_PATH, ctx)
        self.assertNotIn("ctx search", ctx)
        self.assertIn(os.path.join(session, ".claude", "context", "state.json"), ctx)
        self.assertTrue(os.path.isfile(os.path.join(session, ".claude", "context", "state.json")))
        self.assertEqual(os.listdir(outputs), [])

    def cli(self, argv, stdin=""):
        from ctxlc import cli
        real_out, real_in = sys.stdout, sys.stdin
        sys.stdout, sys.stdin = io.StringIO(), io.StringIO(stdin)
        try:
            cli.main(argv)
            return sys.stdout.getvalue()
        finally:
            sys.stdout, sys.stdin = real_out, real_in

    def test_hooks_are_silent_no_ops_without_fcntl(self):
        from ctxlc import store as store_mod
        real = store_mod.fcntl
        store_mod.fcntl = None
        out = io.StringIO()
        real_out, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(hooks.main(io.StringIO(json.dumps(self.base("SessionStart", source="startup")))), 0)
        finally:
            sys.stdout = real_out
            store_mod.fcntl = real
        self.assertEqual(out.getvalue(), "")
        with open(os.path.join(self.store.dir, "errors.log")) as f:
            self.assertIn("needs macOS or Linux", f.read())

    def test_cowork_sync_runs_only_in_a_cloud_task_and_can_be_turned_off(self):
        self.write(rec_user("the report must be a pdf, never a docx.", time.time() - 50))
        ctx = self.run_hook(self.base("SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Cowork state sync", ctx)  # default "auto", but not a cloud Cowork task
        os.makedirs(self.store.dir, exist_ok=True)
        with open(os.path.join(self.store.dir, "config.json"), "w") as f:
            json.dump({"cowork_sync_folder": None}, f)
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "remote_cowork"
        try:
            ctx = self.run_hook(self.base("SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Cowork state sync", ctx)
            self.assertIsNone(self.run_hook(self.base("Stop")))
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT")

    def test_cowork_auto_sync_keeps_state_in_each_tasks_own_folder(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "remote_cowork"
        try:
            self.write(rec_user("the report must be a pdf, never a docx.", time.time() - 50))
            ctx = self.run_hook(self.base("SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
            self.assertIn("cat '<folder>/.claude/ctxlc-cowork.json'", ctx)
            self.assertIn("sync-import --off", ctx)
            self.assertIsNone(self.run_hook(self.base("Stop")))  # no folder registered yet
            with self.assertRaises(SystemExit) as e:
                self.cli(["sync-import", "--folder", "/Users/u/it's", "--none"])
            self.assertIn("single quotes", str(e.exception))

            saved = {"ctxlc_sync": 1, "next_id": {"D": 2}, "next_failure": 1, "brief": None, "handoff": None,
                     "items": [{"id": "D2", "kind": "decision", "text": "Invoices go out on the 1st.", "status": "active",
                                "source": "agent", "created": 1.0, "updated": 1.0}], "failures": []}
            out = self.cli(["sync-import", "--folder", "/Users/u/Projects/billing/"], json.dumps(saved))
            self.assertIn("Invoices go out on the 1st.", out)
            self.assertEqual(self.store.load()["sync"]["folder"], "/Users/u/Projects/billing")
            ctx = self.run_hook(self.base("SessionStart", source="clear"))["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Cowork state sync", ctx)
            self.cli(["note", "decision", "Reminders go out on the 10th."])
            out = self.run_hook(self.base("Stop"))
            self.assertIn("cat > '/Users/u/Projects/billing/.claude/ctxlc-cowork.json' <<'CTXLC_EOF'", out["reason"])
            self.assertNotIn("folder", json.loads(out["reason"].split("\n")[-2]))  # the path is not part of the state
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT")

    def test_cowork_auto_sync_task_without_a_folder_is_not_asked_again(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "remote_cowork"
        try:
            self.write(rec_user("the report must be a pdf, never a docx.", time.time() - 50))
            self.run_hook(self.base("SessionStart", source="startup"))
            self.assertIn("off", self.cli(["sync-import", "--off"]))
            ctx = self.run_hook(self.base("SessionStart", source="compact"))["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Cowork state sync", ctx)
            self.cli(["note", "decision", "Ship weekly on Fridays."])
            self.assertIsNone(self.run_hook(self.base("Stop")))
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT")

    def test_cowork_sync_pulls_then_pushes_only_changed_durable_state(self):
        os.makedirs(self.store.dir, exist_ok=True)
        with open(os.path.join(self.store.dir, "config.json"), "w") as f:
            json.dump({"cowork_sync_folder": "/Users/u/Claude"}, f)
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "remote_cowork"
        try:
            self.write(rec_user("the report must be a pdf, never a docx.", time.time() - 50))
            ctx = self.run_hook(self.base("SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
            self.assertIn("cat '/Users/u/Claude/.claude/ctxlc-cowork.json'", ctx)
            self.assertIn("sync-import", ctx)
            # Before the pull nothing is pushed: it would overwrite the folder's state with this task's.
            self.assertIsNone(self.run_hook(self.base("Stop")))

            saved = {"ctxlc_sync": 1, "next_id": {"D": 4}, "next_failure": 3, "brief": None, "handoff": None,
                     "items": [{"id": "D4", "kind": "decision", "text": "Use Postgres for the ledger.", "status": "active",
                                "source": "agent", "created": 1.0, "updated": 1.0}],
                     "failures": [{"id": "F2", "cmd": "make test", "err": "boom", "ts": 1.0}]}
            out = self.cli(["sync-import"], json.dumps(saved))
            self.assertIn("Use Postgres", out)
            self.assertIn("never a docx", out)
            st = self.store.load()
            ids = {it["text"]: it["id"] for it in st["items"] if it["status"] == "active"}
            self.assertEqual(ids["Use Postgres for the ledger."], "D4")  # IDs survive across tasks
            self.assertEqual(st["failures"][0]["id"], "F2")
            ctx = self.run_hook(self.base("SessionStart", source="clear"))["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Cowork state sync", ctx)  # already pulled in this task

            self.assertIsNone(self.run_hook(self.base("Stop")))  # nothing durable changed since the pull
            self.cli(["note", "decision", "Ship weekly on Fridays."])
            self.assertIsNone(self.run_hook(dict(self.base("Stop"), stop_hook_active=True)))
            out = self.run_hook(self.base("Stop"))
            self.assertEqual(out["decision"], "block")
            self.assertIn("cat > '/Users/u/Claude/.claude/ctxlc-cowork.json' <<'CTXLC_EOF'", out["reason"])
            pushed = json.loads(out["reason"].split("\n")[-2])
            self.assertEqual({it["id"] for it in pushed["items"]} >= {"D4", "D5"}, True)
            self.assertIsNone(self.run_hook(self.base("Stop")))  # asked once per change
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT")

    def test_plugin_zip_carries_sync_setting_only_when_not_default(self):
        import zipfile
        from ctxlc import plugin
        auto = plugin.build(os.path.join(self.proj, "auto.zip"))
        fixed = plugin.build(os.path.join(self.proj, "fixed.zip"), "/Users/u/Claude")
        off = plugin.build(os.path.join(self.proj, "off.zip"), "off")
        self.assertNotIn("ctxlc/plugin_config.json", zipfile.ZipFile(auto).namelist())
        self.assertEqual(json.loads(zipfile.ZipFile(fixed).read("ctxlc/plugin_config.json")),
                         {"cowork_sync_folder": "/Users/u/Claude"})
        self.assertEqual(json.loads(zipfile.ZipFile(off).read("ctxlc/plugin_config.json")),
                         {"cowork_sync_folder": None})

    def test_session_start_injection_stays_under_claude_codes_hook_cap(self):
        # Claude Code shows the model only a 2 KB preview of hook context over 10,000 chars.
        t = time.time() - 5000
        recs = [rec_user(f"for svc{i}, every response must carry the request id header x-req-{i} and never exceed "
                         f"2 seconds, logged with the tenant id and trace id.", t + i) for i in range(400)]
        recs.append(rec_user("now fix the timeout bug in svc3, the p99 is over budget.", t + 500))
        self.write(*recs)
        os.makedirs(self.store.dir, exist_ok=True)
        with open(os.path.join(self.store.dir, "config.json"), "w") as f:
            json.dump({"digest_budget_tokens": 8000}, f)  # a user raising the budget must not cross the cap
        ctx = self.run_hook(self.base("SessionStart", source="compact"))["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(ctx), 10_000)
        self.assertIn("x-req-3 ", ctx)
        self.assertIn("Context protocol", ctx)

    def test_session_start_injects_digest(self):
        self.write(rec_user("Build the billing service; it must use Stripe test mode only.", time.time() - 50))
        out = self.run_hook(self.base("SessionStart", source="clear"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Stripe test mode", ctx)
        self.assertIn("Context protocol", ctx)
        self.assertIsNone(self.run_hook(self.base("SessionStart", source="resume")))

    def test_post_tool_use_rewrites(self):
        text = "\n".join(f"line {i} ok" for i in range(4000)) + "\nFATAL: disk full"
        out = self.run_hook(self.base("PostToolUse", tool_name="Bash", tool_input={"command": "./run"},
                                      tool_response={"stdout": text, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False},
                                      tool_use_id="t1"))
        new = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("FATAL: disk full", new["stdout"])
        self.assertEqual(set(new), {"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"})
        # subagent contexts don't share dedup memory with the main thread
        small = {"stdout": "q" * 900, "stderr": ""}
        self.assertIsNone(self.run_hook(self.base("PostToolUse", tool_name="Bash", tool_input={"command": "c"}, tool_response=small)))
        self.assertIsNone(self.run_hook(self.base("PostToolUse", tool_name="Bash", tool_input={"command": "c"}, tool_response=small, agent_id="sub1")))
        self.assertIsNotNone(self.run_hook(self.base("PostToolUse", tool_name="Bash", tool_input={"command": "c"}, tool_response=small)))

    def test_post_tool_use_cowork_shell(self):
        text = "\n".join(f"line {i} ok" for i in range(4000)) + "\nFATAL: disk full"
        for name, resp in (("mcp__remote-devices__device_bash", [{"type": "text", "text": text}]),
                           ("mcp__workspace__bash", {"content": [{"type": "text", "text": text + " "}], "isError": False}),
                           ("mcp__workspace__bash", text + "  ")):
            out = self.run_hook(self.base("PostToolUse", tool_name=name, tool_input={"command": "./run"},
                                          tool_response=resp, tool_use_id="t-" + name + str(type(resp))))
            new = out["hookSpecificOutput"]["updatedToolOutput"]
            self.assertEqual(type(new), type(resp))
            body = new if isinstance(new, str) else (new["content"] if isinstance(new, dict) else new)[0]["text"]
            self.assertIn("FATAL: disk full", body)
            self.assertLess(len(body), 4000)
        saved = os.path.join(self.proj, "mcp-result.txt")
        with open(saved, "w") as f:
            f.write(text)
        notice = (f"Error: result (75,000 characters across 4,001 lines) exceeds maximum allowed tokens. Output has been "
                  f"saved to {saved}.\nFormat: Plain text\n- For targeted searches: use grep on the file directly.")
        out = self.run_hook(self.base("PostToolUse", tool_name="mcp__remote-devices__device_bash",
                                      tool_input={"command": "./run"}, tool_response=notice, tool_use_id="t-saved"))
        self.assertIn("identical", out["hookSpecificOutput"]["updatedToolOutput"])  # same text as the first call
        image = [{"type": "image", "data": "x" * 9000}]
        self.assertIsNone(self.run_hook(self.base("PostToolUse", tool_name="mcp__workspace__bash",
                                                  tool_input={"command": "c"}, tool_response=image)))
        self.assertIsNone(self.run_hook(self.base("PostToolUse", tool_name="mcp__workspace__web_fetch",
                                                  tool_input={"url": "u"}, tool_response=[{"type": "text", "text": text}])))
        out = self.run_hook(self.base("PreToolUse", tool_name="mcp__remote-devices__device_bash",
                                      tool_input={"command": "npm test", "timeout_ms": 1000}))
        self.assertIn("[ctxlc] exit code", out["hookSpecificOutput"]["updatedInput"]["command"])
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["timeout_ms"], 1000)

    def test_idle_guard_blocks_once(self):
        self.write(rec_asst([{"type": "text", "text": "done"}], time.time() - 5 * 3600, ctx=250000))
        out = self.run_hook(self.base("UserPromptSubmit", prompt="continue"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("250k", out["reason"])
        self.assertIn("new session", out["reason"])  # keeps the old chat visible, unlike /clear
        self.assertIsNone(self.run_hook(self.base("UserPromptSubmit", prompt="continue")))

    def test_idle_guard_quiet_when_warm_or_small(self):
        self.write(rec_asst([{"type": "text", "text": "done"}], time.time() - 60, ctx=250000))
        self.assertIsNone(self.run_hook(self.base("UserPromptSubmit", prompt="go")))
        self.write(rec_asst([{"type": "text", "text": "done"}], time.time() - 9 * 3600, ctx=20000))
        self.assertIsNone(self.run_hook(self.base("UserPromptSubmit", prompt="go")))

    def test_model_switch_guard(self):
        p = self.base("PreModelSwitch", from_model="claude-fable-5-1", to_model="claude-opus-5-5", context_tokens=300000,
                      prompt_cache_warm=True, estimated_cache_write_usd=3.0, source="command", cache_ttl="1h")
        out = self.run_hook(p)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        p.update(context_tokens=20000)
        self.assertIsNone(self.run_hook(p))

    def test_handoff_consumed_by_first_session(self):
        self.write(rec_user("Build the billing service.", time.time() - 50))
        with self.store.transaction() as st:
            st["handoff"] = {"ts": time.time(), "model": "m", "text": "Next: wire Stripe webhooks"}
        ctx = lambda sid, src: self.run_hook(dict(self.base("SessionStart", source=src), session_id=sid))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Stripe webhooks", ctx("s2", "startup"))
        self.assertIn("Stripe webhooks", ctx("s2", "compact"))
        with self.store.transaction() as st:
            st["handoff"]["consumed_at"] -= 7 * 3600
        self.assertNotIn("Stripe webhooks", ctx("s3", "startup"))

    def test_idle_guard_uses_last_cache_write_ttl(self):
        old = time.time() - 600  # 10 min idle: expired for a 5-min cache, warm for 1h
        r1 = rec_asst([{"type": "text", "text": "a"}], old - 60, ctx=250000)
        r1["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 1000}
        r2 = rec_asst([{"type": "text", "text": "b"}], old, ctx=250000)
        r2["message"]["usage"]["cache_creation"] = {}
        self.write(r1, r2)
        self.assertEqual(hooks.last_turn_info(self.transcript)["ttl"], 300)
        self.assertEqual(self.run_hook(self.base("UserPromptSubmit", prompt="go"))["decision"], "block")

    def test_idle_guard_lets_ctx_commands_through(self):
        self.write(rec_asst([{"type": "text", "text": "done"}], time.time() - 5 * 3600, ctx=250000))
        self.assertIsNone(self.run_hook(self.base("UserPromptSubmit", prompt="ctx reset")))

    def test_store_is_private_and_gc(self):
        self.store.ensure()
        self.assertEqual(os.stat(self.store.dir).st_mode & 0o777, 0o700)
        _, old = toolout.archive(self.store, "old output", "$ x")
        _, new = toolout.archive(self.store, "new output", "$ y")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        with open(self.store.history_path, "w") as f:
            for i in range(2000):
                f.write(json.dumps({"i": i, "pad": "p" * 50}) + "\n")
        removed, dropped = self.store.gc(14, 60_000)
        self.assertEqual((removed, os.path.exists(old), os.path.exists(new)), (1, False, True))
        recs = list(self.store.iter_jsonl(self.store.history_path))
        self.assertGreater(dropped, 0)
        self.assertEqual(recs[-1]["i"], 1999)
        self.assertEqual(len(recs), recs[-1]["i"] - recs[0]["i"] + 1)  # whole lines only

    def test_install_roundtrip_keeps_user_settings(self):
        from ctxlc import cli
        settings = os.path.join(self.proj, ".claude", "settings.local.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w") as f:
            json.dump({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}, f)
        real = sys.stdout
        sys.stdout = io.StringIO()
        try:
            cli.main(["install", "--project", self.proj, "--no-skill"])
            cli.main(["install", "--project", self.proj, "--no-skill"])  # idempotent
            s = json.load(open(settings))
            self.assertEqual(len(s["hooks"]["Stop"]), 2)
            # the second install changes nothing, so the backup still holds the pre-install settings
            self.assertEqual(json.load(open(settings + ".ctxlc.bak")),
                             {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})
            cli.main(["uninstall", "--project", self.proj, "--no-skill"])
            self.assertEqual(json.load(open(settings)), {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})
            os.remove(settings)
            cli.main(["uninstall", "--project", self.proj, "--no-skill"])  # missing file: no crash
            with open(settings, "w") as f:
                f.write("{broken")
            with self.assertRaises(SystemExit):
                cli.main(["install", "--project", self.proj, "--no-skill"])
            self.assertEqual(open(settings).read(), "{broken")
        finally:
            sys.stdout = real

    def test_install_leaves_auto_compact_window_alone_unless_asked(self):
        from ctxlc import cli
        settings = os.path.join(self.proj, ".claude", "settings.local.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w") as f:
            json.dump({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "160000", "FOO": "1"}}, f)  # written by an earlier ctxlc
        real = sys.stdout
        sys.stdout = io.StringIO()
        try:
            cli.main(["install", "--project", self.proj, "--no-skill"])
            self.assertEqual(json.load(open(settings))["env"], {"FOO": "1"})
            cli.main(["install", "--project", self.proj, "--no-skill", "--window", "400000"])
            self.assertEqual(json.load(open(settings))["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "400000")
            cli.main(["uninstall", "--project", self.proj, "--no-skill", "--window", "400000"])
            self.assertEqual(json.load(open(settings)), {"env": {"FOO": "1"}})
        finally:
            sys.stdout = real

    def test_post_compact_saves_summary(self):
        self.run_hook(self.base("PostCompact", trigger="auto", compact_summary="Summary: migrating auth to OAuth"))
        st = self.store.load()
        self.assertIn("OAuth", st["last_compact_summary"]["text"])
        self.assertEqual(retrieve.search(self.store, st, "oauth")[0]["label"], "compact_summary")

    def test_pre_compact_tells_the_summary_what_is_kept(self):
        t = time.time()
        self.write(rec_asst([{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "make test"}}], t - 9),
                   rec_result("b1", "1 failed, 3 passed", t - 9, True),
                   rec_user("Payments must go through Stripe test mode only.", t - 5))
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            hooks.main(io.StringIO(json.dumps(self.base("PreCompact", trigger="auto", custom_instructions=""))))
        finally:
            sys.stdout = real
        text = out.getvalue()
        self.assertIn("- R1 (requirement): Payments must go through Stripe test mode only.", text)
        self.assertIn("- F1 (open failure): `make test`", text)
        self.assertIn("refer to these by ID", text)
        self.assertEqual(self.store.load()["items"][0]["id"], "R1")  # ingested before compaction


class TestHistory(Base):
    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self.real_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home.name
        slug = "".join(c if c.isalnum() else "-" for c in os.path.abspath(self.proj))
        self.tdir = os.path.join(self.home.name, ".claude", "projects", slug)
        os.makedirs(self.tdir)

    def tearDown(self):
        os.environ["HOME"] = self.real_home
        self.home.cleanup()
        super().tearDown()

    def conv(self, sid, recs):
        with open(os.path.join(self.tdir, sid + ".jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def test_lists_cleared_conversations_newest_first_and_renders_them(self):
        t = time.time() - 86400
        self.conv("aaaa1111", [
            rec_user("build the <parser>", t),
            rec_asst([{"type": "text", "text": "Reading it."}, {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "ls -la\nmore"}}], t + 1),
            rec_result("x", "files", t + 2),
            rec_asst([{"type": "text", "text": "Done."}], t + 3),
            rec_user("ctx panel", t + 4),
            {"type": "user", "timestamp": iso(t + 5), "isCompactSummary": True, "message": {"role": "user", "content": "summary"}},
        ])
        self.conv("bbbb2222", [rec_user("second chat", t + 100), rec_asst([{"type": "text", "text": "ok"}], t + 101)])
        self.conv("cccc3333", [{"type": "summary", "summary": "no prompts"}])
        convs = history.conversations(self.proj)
        self.assertEqual([c["id"] for c in convs], ["bbbb2222", "aaaa1111"])  # no-prompt file skipped
        self.assertEqual(convs[1]["prompts"], 1)  # ctx control prompt not counted
        self.assertEqual(history.find(self.proj, "2")["id"], "aaaa1111")
        self.assertEqual(history.find(self.proj, "bbbb")["id"], "bbbb2222")
        self.assertIsNone(history.find(self.proj, "zz"))
        md = history.markdown(convs[1])
        self.assertIn("build the <parser>", md)
        self.assertIn("_1 tool call_", md)
        self.assertIn("compacted here", md)
        page = history.render(convs[1])
        self.assertIn("build the &lt;parser&gt;", page)
        self.assertIn("Bash: ls -la</div>", page)  # one line per tool call
        self.assertNotIn("<parser>", page)


if __name__ == "__main__":
    unittest.main()
