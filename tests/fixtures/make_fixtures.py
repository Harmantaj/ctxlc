"""Writes the synthetic Claude Code transcripts in this folder, so CI and tests/e2e.py need no private history.

Usage: python3 tests/fixtures/make_fixtures.py   (deterministic: rerunning reproduces the committed files)

The project, people and text are invented ("lumen", an LLM gateway). Records follow the shape Claude Code 2.1.278
writes, including record types ctxlc ignores (queue operations, attachments, file-history snapshots, thinking
blocks, hook summaries, cost state) so the parser is exercised against realistic noise. Each transcript also holds
traps that must not reach the project state; tests/test_fixtures.py checks both what is kept and what is not.

  brief_session.jsonl  a pasted brief, a failing build, a decision superseded once, a casual preference, a test
                       run with two failures of which the user reports one fixed, a TodoWrite, then a compaction.
                       Traps: a sidechain (subagent) prompt with a decision, a compact summary longer than the
                       brief, an isMeta prompt, a <synthetic> assistant turn, a /model command, a permission denial.
  model_switch.jsonl   a later session: the model changes from Opus to Sonnet, a piped test run hides a failure
                       behind `| tail`, a later full run passes, and the current task is restated.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CWD = "/home/dev/lumen"
VERSION = "2.1.278"
T0 = 1780300800  # 2026-06-01 08:00:00 UTC


def iso(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


class Transcript:
    def __init__(self, session, t0, model):
        self.session, self.t, self.model, self.n, self.recs, self.parent = session, t0, model, 0, [], None

    def _uuid(self):
        self.n += 1
        return f"{self.session[:8]}-0000-4000-8000-{self.n:012d}"

    def _base(self, typ, dt, **kw):
        self.t += dt
        u = self._uuid()
        r = dict(parentUuid=self.parent, isSidechain=False, userType="external", cwd=CWD, sessionId=self.session,
                 version=VERSION, gitBranch="main", entrypoint="cli", type=typ, uuid=u, timestamp=iso(self.t))
        r.update(kw)
        self.parent = u
        self.recs.append(r)
        return r

    def raw(self, rec):
        self.recs.append(rec)

    def user(self, text, dt=30, **kw):
        return self._base("user", dt, message={"role": "user", "content": text}, permissionMode="default",
                          promptId=f"p{self.n}", **kw)

    def assistant(self, blocks, dt=8, ctx=60000, model=None, ttl="1h"):
        model = model or self.model
        usage = {"input_tokens": 3, "cache_creation_input_tokens": 1200, "cache_read_input_tokens": ctx - 1203,
                 "output_tokens": 180, "cache_creation": {f"ephemeral_{ttl}_input_tokens": 1200}}
        msg = {"id": f"msg_{self.n:04d}", "type": "message", "role": "assistant", "model": model, "content": blocks,
               "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
               "stop_sequence": None, "usage": usage}
        return self._base("assistant", dt, message=msg, apiBlockIndex=0, requestId=f"req_{self.n:04d}")

    def tool(self, tool_id, name, inp, text, is_error=False, dt=4, ctx=60000):
        self.assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": inp, "caller": "assistant"}], ctx=ctx)
        return self._base("user", dt, message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}]},
            toolUseResult={"stdout": text, "stderr": "", "interrupted": False}, sourceToolAssistantUUID=self.parent)

    def system(self, subtype, dt=1, **kw):
        return self._base("system", dt, subtype=subtype, level="info", **kw)

    def write(self, name):
        with open(os.path.join(HERE, name), "w") as f:
            for r in self.recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


BRIEF = """# lumen — product brief

lumen is an internal LLM gateway. Product teams send chat requests to lumen instead of calling model providers
directly, and lumen handles routing, context optimization, caching and cost reporting for every tenant.

## Problem
Teams run long agent sessions that resend the whole conversation on every call. A model switch in the middle of a
session throws the provider's prompt cache away, so the next request pays to write the full context again. Nobody
can see which tenant is spending what, and two teams have already leaked API keys into request logs.

## Goals
- Context optimization: trim tool output and stale turns before they reach the provider, without losing the facts
  the model still needs.
- Model switch support: when a tenant moves a session from one model to another, send a compact summary of the
  session instead of the raw history.
- Per-tenant cost reporting, refreshed every minute.

## Requirements
Every tenant must authenticate with its own API key. lumen must never log request or response bodies; it logs
token counts and latency only. Responses must stream; buffering a whole completion is not acceptable. The service
has to run on Node 22 with TypeScript in strict mode.

## Non-goals
lumen does not fine-tune models, host models, or store conversations beyond the context optimization cache, which
expires after 24 hours.

## Milestones
1. Proxy with authentication and streaming.
2. Context optimization for tool output.
3. Model switch summaries.
4. Cost dashboard.
"""

COMPACT_SUMMARY = ("This session is being continued from a previous conversation that ran out of context. Summary: "
                   + "The user asked to build lumen and we discussed the decision to use SQLite for the cache. "
                   + "Earlier work covered the gateway skeleton, routing tables and the streaming proxy. " * 60)

TSC_FAIL = """> lumen@0.1.0 build
> tsc -p tsconfig.json

src/router.ts(88,5): error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.
Found 1 error in src/router.ts:88

Exit code 2"""

TEST_FAIL = """> lumen@0.1.0 test
> vitest run

 ✓ src/auth/keys.test.ts (6 tests)
 ✓ src/proxy/headers.test.ts (4 tests)
 ❯ src/proxy/stream.test.ts (3 tests | 1 failed)
   × streams chunks in order
     AssertionError: expected [ 'b', 'a' ] to deeply equal [ 'a', 'b' ]
 ❯ src/billing/meter.test.ts (5 tests | 1 failed)
   × counts cached tokens at the discounted rate
     AssertionError: expected 1200 to be 120

 Test Files  2 failed | 2 passed (4)
      Tests  2 failed | 16 passed (18)

Exit code 1"""


def brief_session():
    s = Transcript("5b0d1c2e-7a41-4f0e-9d0c-3a1e2f4b6c71", T0, "claude-opus-5-5")
    s.raw({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(T0), "sessionId": s.session,
           "content": "Here's the spec"})
    s.raw({"type": "file-history-snapshot", "messageId": "m0", "isSnapshotUpdate": False,
           "snapshot": {"messageId": "m0", "trackedFileBackups": {}, "timestamp": iso(T0)}})
    s.user(f"<pasted_content id=\"p1\">\n{BRIEF}</pasted_content>\n\nHere's the spec for lumen. Read it and set up the "
           "project skeleton. <system-reminder>The user's timezone is UTC.</system-reminder>")
    s._base("attachment", 1, attachment={"type": "todo_reminder", "content": []}, rendered="")
    s.assistant([{"type": "thinking", "thinking": "Plan the skeleton: package.json, tsconfig, src/.", "signature": "sig"},
                 {"type": "text", "text": "I'll scaffold lumen with a strict TypeScript config and a router, then build it."}])
    s.tool("w1", "Write", {"file_path": f"{CWD}/src/router.ts", "content": "export function route() {}\n"}, "File created")
    s.tool("t1", "TodoWrite", {"todos": [{"content": "Proxy with auth and streaming", "status": "in_progress",
                                         "activeForm": "Building the proxy"},
                                        {"content": "Context optimization for tool output", "status": "pending",
                                         "activeForm": "Adding context optimization"}]}, "Todos updated")
    s.tool("b1", "Bash", {"command": "npm run build", "description": "Type-check and build"}, TSC_FAIL, is_error=True)
    s.assistant([{"type": "text", "text": "The build fails on one strict-null error in src/router.ts line 88."}])
    s.user("For now we'll use SQLite for the context optimization cache. Fix the router error next.")
    s.tool("r1", "Read", {"file_path": f"{CWD}/src/router.ts"}, "1\texport function route() {}")
    s.tool("e1", "Edit", {"file_path": f"{CWD}/src/router.ts", "old_string": "a", "new_string": "b"}, "File updated")
    s.tool("b2", "Bash", {"command": "npm run build"}, "> lumen@0.1.0 build\n> tsc -p tsconfig.json\n")
    s.assistant([{"type": "text", "text": "The build passes now."}])
    s.system("stop_hook_summary", hookCount=1, hookInfos=[], hookErrors=[], preventedContinuation=False,
             stopReason="", hasOutput=False)
    s.tool("b3", "Bash", {"command": "npm test"}, TEST_FAIL, is_error=True)
    s.assistant([{"type": "text", "text": "Two tests fail: chunk ordering in the stream test and the cached-token rate."}])
    s.user("Oh, and I can't stand default exports — keep them out of this codebase, named exports only.")
    s.user("Decision change: we're switching the context optimization cache from SQLite to Redis instead, because "
           "SQLite locks under concurrent tenants.")
    s.user("I fixed the stream ordering test myself, so that failure is fixed. The cached-token rate one is still open.")
    # Traps -------------------------------------------------------------------------------------------------------
    sidechain = s.user("Subagent: we'll use MongoDB for the cost dashboard.", isSidechain=True)
    sidechain["isSidechain"] = True
    s.user("Caveat: the messages below were generated by the user running local commands.", isMeta=True)
    s.user("<command-name>/model</command-name>\n<command-message>model</command-message>")
    s.assistant([{"type": "text", "text": "No response requested."}], model="<synthetic>")
    s.tool("b4", "Bash", {"command": "rm -rf node_modules"},
           "Permission for this action was denied by the Claude Code auto mode classifier.", is_error=True)
    s.system("compact_boundary", content="Conversation compacted",
             compactMetadata={"trigger": "auto", "preTokens": 151000, "postTokens": 9000})
    s.user(COMPACT_SUMMARY, isCompactSummary=True)
    # -------------------------------------------------------------------------------------------------------------
    s.user("Next, model switch support is the current task: summarise a session when a tenant changes models.")
    s.assistant([{"type": "text", "text": "Starting model switch summaries: on a switch, lumen will send a compact "
                                          "summary of the session instead of replaying the raw history."}])
    s.raw({"type": "cost-state", "sessionId": s.session, "totalCostUSD": 1.92, "modelUsage": {},
           "hasUnknownModelCost": False, "startTime": iso(T0), "totalDuration": 0, "totalAPIDuration": 0,
           "totalAPIDurationWithoutRetries": 0, "totalToolDuration": 0, "totalLinesAdded": 12, "totalLinesRemoved": 1})
    s.raw({"type": "last-prompt", "sessionId": s.session, "lastPrompt": "Next, model switch support…",
           "leafUuid": s.parent})
    s.write("brief_session.jsonl")


def model_switch():
    s = Transcript("9c4e7f10-2b3d-4a5e-8f60-71829a0b1c2d", T0 + 86400, "claude-opus-5-5")
    s.user("Continue with the model switch summaries for lumen.")
    s.assistant([{"type": "text", "text": "Picking up model switch summaries."}], ctx=40000)
    s.user("<command-name>/model</command-name>\n<command-args>claude-sonnet-5-5</command-args>")
    s.assistant([{"type": "text", "text": "Running the tests on the new model."}], model="claude-sonnet-5-5", ctx=42000,
                ttl="5m")
    s.tool("b1", "Bash", {"command": "npm test 2>&1 | tail -4"},
           " Test Files  1 failed | 3 passed (4)\n      Tests  1 failed | 17 passed (18)\nnpm ERR! Test failed.", ctx=43000)
    s.assistant([{"type": "text", "text": "One test still fails; the tail hides which one."}],
                model="claude-sonnet-5-5", ctx=43500)
    s.tool("b2", "Bash", {"command": "npm test"}, " Test Files  4 passed (4)\n      Tests  18 passed (18)", ctx=44000)
    s.assistant([{"type": "text", "text": "All 18 tests pass now, including the cached-token rate test."}],
                model="claude-sonnet-5-5", ctx=44500, ttl="5m")
    s.write("model_switch.jsonl")


if __name__ == "__main__":
    brief_session()
    model_switch()
    print("wrote brief_session.jsonl and model_switch.jsonl")
