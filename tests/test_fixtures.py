"""Regression tests over the synthetic transcripts in tests/fixtures (regenerate with tests/fixtures/make_fixtures.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxlc import ingest  # noqa: E402
from ctxlc.store import empty_state, render_digest  # noqa: E402
from tests.test_ctxlc import Base  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class TestFixtures(Base):
    def ingest(self, *names):
        st = empty_state(self.proj)
        for n in names:
            ingest.ingest_transcript(self.store, st, os.path.join(FIXTURES, n + ".jsonl"))
        return st

    def active(self, st, kind):
        return [i["text"] for i in st["items"] if i["kind"] == kind and i["status"] == "active"]

    def test_brief_is_the_paste_not_the_compact_summary(self):
        st = self.ingest("brief_session")
        self.assertTrue(st["brief"]["text"].startswith("# lumen — product brief"))
        self.assertNotIn("<system-reminder>", st["brief"]["text"])
        self.assertNotIn("timezone", st["brief"]["text"])
        self.assertIn("Defining brief", render_digest(st))

    def test_traps_stay_out_of_state(self):
        st = self.ingest("brief_session")
        texts = " ".join(i["text"] for i in st["items"]) + " ".join(p if isinstance(p, str) else p.get("text", "")
                                                                   for p in st.get("prompts", []))
        for trap in ("MongoDB", "Subagent", "ran out of context", "Caveat:", "/model", "No response requested"):
            self.assertNotIn(trap, texts)
        self.assertNotIn("rm -rf", [f["cmd"] for f in st["failures"]])  # a permission denial is not a failure

    def test_requirements_and_casual_preference(self):
        reqs = " ".join(self.active(self.ingest("brief_session"), "requirement"))
        for needle in ("own API key", "never log request or response bodies", "Responses must stream", "Node 22",
                       "can't stand default exports"):
            self.assertIn(needle, reqs)

    def test_decision_superseded(self):
        st = self.ingest("brief_session")
        sqlite = [i for i in st["items"] if i["text"].startswith("For now we'll use SQLite")]
        self.assertEqual([i["status"] for i in sqlite], ["superseded"])
        self.assertTrue(any("from SQLite to Redis" in t for t in self.active(st, "decision")))

    def test_brief_descriptions_do_not_supersede_each_other(self):
        # Descriptive "instead of" sentences in the brief were once decisions, and one superseded the other through a
        # single shared word.
        st = self.ingest("brief_session")
        self.assertEqual([i["text"][:30] for i in st["items"] if i["status"] == "superseded"],
                         ["For now we'll use SQLite for t"])

    def test_failures_and_user_reported_fix(self):
        st = self.ingest("brief_session")
        build, test = st["failures"]
        self.assertEqual((build["cmd"], build["resolved"]), ("npm run build", True))  # the rebuild passed
        self.assertIn("src/router.ts(88,5): error TS2345", build["err"])
        self.assertEqual((test["cmd"], test["resolved"]), ("npm test", False))
        self.assertEqual(test["fixed_lines"], ["❯ src/proxy/stream.test.ts (3 tests | 1 failed)"])
        self.assertIn("expected 1200 to be 120", test["err"])

    def test_session_model_ttl_and_tools(self):
        st = self.ingest("brief_session")
        s = st["sessions"]["brief_session"]
        self.assertEqual((s["model"], s["ttl"], s["last_ctx"]), ("claude-opus-5-5", 3600, 60000))  # <synthetic> ignored
        self.assertEqual(st["files"]["/home/dev/lumen/src/router.ts"]["reads"], 1)
        self.assertEqual([t["status"] for t in st["todos"]["items"]], ["in_progress", "pending"])

    def test_model_switch_and_piped_failure(self):
        st = self.ingest("brief_session")
        test = st["failures"][1]
        ingest.ingest_transcript(self.store, st, os.path.join(FIXTURES, "model_switch.jsonl"))
        s = st["sessions"]["model_switch"]
        self.assertEqual((s["model"], s["ttl"]), ("claude-sonnet-5-5", 300))
        self.assertEqual(st["last_status"]["model"], "claude-sonnet-5-5")
        # `npm test | tail` exits 0 but reports a failure: it adds to the open `npm test` failure, not a new one,
        # and the later full `npm test` that passes resolves it.
        self.assertEqual(len(st["failures"]), 2)
        self.assertIn("1 failed | 17 passed", test["err"])
        self.assertTrue(test["resolved"])


if __name__ == "__main__":
    unittest.main()
