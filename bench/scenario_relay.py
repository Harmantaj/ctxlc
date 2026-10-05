"""Second synthetic project for vs_baselines.py: `beacon`, a webhook-relay service in TypeScript.

Same shape as the ledgr scenario (a work session, then a 13-question quiz), different wording and harder traps,
written without looking at what ctxlc extracts so it measures generalisation rather than tuning:

  * a decision superseded twice (Redis, then NATS, then Kafka);
  * a preference with no "I hate / can't stand" marker ("no ORMs, ever");
  * fixes reported in other words ("is green now") and by a third person ("Marco fixed that too");
  * jest-style test output instead of pytest, a tsc build instead of make;
  * a detail that only ever appeared in test output (the status code the expired-token test received).
"""

import os
import re
import stat

DEV_PY = r'''#!/usr/bin/env python3
import sys
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "build":
    print("> beacon@0.4.0 build")
    print("> tsc -p tsconfig.json")
    for i in range(3500):
        if i == 977:
            print("src/routes/invoices.ts(142,7): error TS2322: Type 'string' is not assignable to type 'number'.")
        elif i == 2410:
            print("src/queue/redis.ts(31,12): error TS2307: Cannot find module 'ioredis' or its corresponding type declarations.")
        else:
            print(f"[{i:04d}/3500] emit src/mod_{i % 83}/unit_{i}.ts -> dist/mod_{i % 83}/unit_{i}.js")
    print("Found 2 errors in 2 files.")
    sys.exit(2)
if mode == "test":
    print("> beacon@0.4.0 test")
    print("> jest --ci")
    for i in range(415):
        if i == 61:
            print("FAIL src/auth/token.spec.ts")
            print("  ● auth › rejects expired tokens")
            print("    expect(received).toBe(expected) // Object.is equality")
            print("    Expected: 401")
            print("    Received: 200")
        elif i == 230:
            print("FAIL src/ratelimit/limiter.spec.ts")
            print("  ● limiter › releases tokens after the window")
            print('    thrown: "Exceeded timeout of 5000 ms for a test."')
        elif i == 333:
            print("FAIL src/webhooks/signature.spec.ts")
            print("  ● webhooks › verifies the HMAC signature")
            print("    Error: ENOENT: no such file or directory, open 'fixtures/webhook_payload.json'")
        else:
            print(f"PASS src/mod_{i % 83}/unit_{i}.spec.ts")
    print("Test Suites: 3 failed, 412 passed, 415 total")
    print("Tests:       3 failed, 1630 passed, 1633 total")
    sys.exit(1)
print("usage: ./dev build|test")
sys.exit(64)
'''

LOREM = ("Beacon accepts outbound webhook events from internal services, signs each payload with the tenant's HMAC "
         "secret and delivers it to the subscriber's endpoint. Failed deliveries are retried with exponential backoff "
         "and jitter, and every attempt is written to an append-only delivery log that operators can search. ")

WORK = [
    "We're starting `beacon`, an internal webhook-relay service in TypeScript on Node 20. The service has to stay "
    "named beacon — the ops dashboards key on that name. Every endpoint requires an API key, except GET /healthz. "
    "We'll queue deliveries in Redis. Read docs/spec_1.md and give me a 3-bullet summary.",
    "Run `./dev build` and list the type errors (file, line and message).",
    "Read docs/spec_2.md and give me 3 bullets on the retry policy. Btw, no ORMs, ever — I've been burned by Prisma "
    "migrations twice, we write the SQL by hand.",
    "Run `./dev test` and tell me what failed.",
    "Change of plan on the queue: we're moving from Redis to NATS JetStream, because we need replayable delivery "
    "logs. Read docs/spec_3.md and summarise it in 2 bullets.",
    "Marco bumped the limiter's test timeout this morning and that one is green now. The webhook signature failure "
    "was just a missing fixture file; Marco fixed that too. The expired-token one is still broken.",
    "On the expired-token bug: we tried widening the clock-skew leeway to 5 minutes and security vetoed it, because "
    "it lets revoked keys through for too long. Compare exp against the server's monotonic clock with a 30-second "
    "leeway instead. Don't implement it yet.",
    "Scratch NATS — the platform team only supports Kafka for us, so we're going with Kafka after all. The replay "
    "requirement still stands.",
    "The current task is per-tenant rate limiting: 120 requests per minute per API key, with a burst of 20. Read "
    "docs/spec_4.md, then outline the implementation in 4 bullets. No code yet. Also, Tomás owns the deploy and the "
    "release freeze starts Thursday.",
]

QUIZ = (
    "Quick check before we continue. Answer from what you know about this project. You may use tools, but do not run "
    "./dev and do not open files under docs/. Reply as a numbered list, one short line each:\n"
    "1) What must the service be named?\n2) Which endpoints need an API key?\n3) Which queue are we using now?\n"
    "4) Which queues did we consider before, and why did we move off each?\n"
    "5) Is there any kind of library I asked you to keep out of the project?\n"
    "6) Which test failures are still open right now?\n"
    "7) Which fix for the expired-token bug was rejected, and why?\n8) Which approach should we use instead?\n"
    "9) What is the current task?\n10) What are the exact rate-limit numbers?\n"
    "11) In `./dev build`, which file and line had the 'string' is not assignable to 'number' error?\n"
    "12) In the test run, what status code did the expired-token test receive instead of 401?\n"
    "13) Who owns the deploy, and when does the freeze start?"
)


def _has(*pats):
    return lambda a: all(re.search(p, a) for p in pats)


def _current_queue(a):
    k = a.find("kafka")
    return k >= 0 and all(a.find(x) < 0 or k < a.find(x) for x in ("nats", "redis"))


def _open_failures(a):
    fixed = r"fixed|green|pass|resolved|closed|not open|no longer"
    return (re.search(r"expir|token", a)
            and (not re.search(r"limiter|rate|timeout", a) or re.search(fixed, a))
            and (not re.search(r"webhook|signature|hmac|fixture", a) or re.search(fixed, a)))


CHECKS = {
    1: ("service name", _has(r"beacon")),
    2: ("auth rule + exception", _has(r"healthz")),
    3: ("current queue (superseded twice)", _current_queue),
    4: ("previous queues + reasons", _has(r"redis", r"nats", r"replay", r"platform|support")),
    5: ("unmarked preference: no ORMs", _has(r"\borms?\b|prisma")),
    6: ("open failures (two were fixed)", _open_failures),
    7: ("rejected fix + reason", _has(r"leeway|skew", r"revoked")),
    8: ("chosen approach", _has(r"monotonic", r"30")),
    9: ("current task", _has(r"rate[ -]?limit")),
    10: ("exact numbers", _has(r"\b120\b", r"\b20\b")),
    11: ("build detail only in output", _has(r"invoices\.ts\D{0,8}142")),
    12: ("test detail only in output", _has(r"\b200\b")),
    13: ("owner + day", _has(r"tom[aá]s", r"thursday")),
}


def doc(name, chars):
    body, i = [], 0
    while sum(map(len, body)) < chars:
        body.append(f"\n## Section {i} of {name}\n" + LOREM * 3)
        i += 1
    return f"# {name}\n" + "".join(body)


def setup(path, with_ctxlc=False, doc_chars=25000, n_docs=4):
    assert not with_ctxlc, "vs_baselines installs ctxlc itself, after the work session"
    os.makedirs(os.path.join(path, "docs"), exist_ok=True)
    dev = os.path.join(path, "dev")
    with open(dev, "w") as f:
        f.write(DEV_PY)
    os.chmod(dev, os.stat(dev).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    with open(os.path.join(path, "package.json"), "w") as f:
        f.write('{\n  "name": "beacon",\n  "version": "0.4.0",\n  "private": true\n}\n')
    for i in range(1, n_docs + 1):
        with open(os.path.join(path, "docs", f"spec_{i}.md"), "w") as f:
            f.write(doc(f"spec_{i}", doc_chars))
