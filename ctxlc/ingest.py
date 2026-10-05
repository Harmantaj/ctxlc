"""Incremental, deterministic transcript ingestion (no model calls).

Each transcript has a byte cursor in state["cursors"]; only lines appended since
the last run are parsed, so the cost is proportional to new activity, not to
session length.
"""

import datetime
import json
import os
import re

from . import toolout
from .store import add_item, content_words, next_failure_id

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SKIP_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-",
    "<bash-input>",
    "<bash-stdout>",
    "<task-notification>",
    "[Request interrupted",
    "Stop hook feedback",
)
# Imperative at the start of a sentence, or a modal obligation anywhere in it.
DIRECTIVE_START = re.compile(r"^(please\s+)?(always|never|do not|don't|make sure|ensure|avoid|must|only|keep|use)\b", re.I)
DIRECTIVE_MODAL = re.compile(r"\b(must(?: not)?|should(?: not)?|shouldn't|needs? to|has to|have to|is required to|are required to)\b", re.I)
# Mid-sentence directives ("Oh, and I can't stand pandas — keep it out of this project"): a narrower set at the
# start of a clause, plus first-person preferences anywhere.
CLAUSE_SPLIT = re.compile(r"\s+[—–]\s+|\s+-\s+|;\s*|,\s+(?:and\s+|but\s+|so\s+)?")
CLAUSE_DIRECTIVE = re.compile(r"^(please\s+)?(always|never|do not|don't|make sure|ensure|avoid|stop using|stay away from|"
                              r"keep (?:it|them|this|that|\w+) (?:out|away|off))\b", re.I)
# "X is always injected, and never expires": a predicate sharing the sentence's subject describes it. The verb
# form tells it apart from "…, and never commit secrets"; a bare comma ("cents, never floats") is left alone.
COORD_DESCRIPTIVE = re.compile(r",\s+(?:and|but)\s+(?:always|never)\s+(?:(?:is|are|was|were|has|had|does|did|gets|got)\b|"
                               r"\w+(?<![su])s\b|\w+(?<!e)(?<!mb)ed\b)", re.I)
# "Every endpoint requires an API key": a rule over a whole class of things.
UNIVERSAL_RULE = re.compile(r"^(?:every|each|all|any)\s+(?:[\w/-]+\s+){1,3}?(?:requires?|needs?|must|gets?)\b", re.I)
# "no ORMs, ever" / "no mocks in these tests": a prohibition with no verb.
PROHIBITION = re.compile(r"(?:^|[,—–;]\s*)(?:and\s+)?no\s+[\w-]+(?:\s+[\w-]+)?,?\s+(?:ever|anywhere|at all|in (?:this|the) (?:project|repo|codebase))\b", re.I)
# A one-shot instruction ("don't implement it yet", "skip the tests for now") is not a standing requirement.
TRANSIENT = re.compile(r"\b(?:yet|for now|right now|this time|today)\s*[.!]?$", re.I)
# "The current task is …", "Next task: …".
TASK = re.compile(r"^(?:ok(?:ay)?,?\s+|so,?\s+)?(?:the\s+)?(?:current|next|new)\s+task\s+(?:is|will be)\s+(.{8,})", re.I)
PREFERENCE = re.compile(r"\bI(?: really| personally)? (?:can't stand|cannot stand|hate|dislike|don't (?:want|like)|"
                        r"do not (?:want|like)|prefer|would rather)\b|\bI'd rather\b", re.I)
# "the unicode bug is fixed now": a user-reported fix of part of a recorded failure.
FIX_CLAIM = re.compile(r"\b(fixed|patched|resolved|solved|sorted out|works now|is working now|passes now|now passes|"
                       r"(?:is|are) (?:green|passing) now|green now|now green)\b", re.I)
FIX_NEGATED = re.compile(r"\b(not|never|still|yet|no one)\b|n't\b", re.I)
FIX_CLAUSES = re.compile(r"\s+[—–]\s+|\s+-\s+|;\s*|(?<=[.!?])\s+|\bbut\b|\bwhile\b|\bexcept\b", re.I)
FIX_STOP = set("fixed fix fixes patched patch resolved resolve solved solve sorted works working passes pass now just "
               "myself machine local locally that this those these failure failures failing fail failed test tests "
               "error errors issue issues bug bugs problem with from have been already also the and which what there "
               "their since after before when then your our mine".split())
# "may need to …" describes a possibility, not an obligation.
HEDGED = re.compile(r"\b(?:may|might|could|would)\s+(?:need|have|has)\s+to\b", re.I)
# "We'll queue deliveries in Redis": a plan naming a technology (capitalised), not "we'll look at it in a bit".
TECH_PLAN = re.compile(r"\b[Ww]e(?:'ll| will| are going to)\s+(?:[a-z]+\s+){1,3}?(?:in|on|with|using|via|through)\s+[A-Z][\w.+-]*")
DECISION = re.compile(
    r"\b(decision|decided|we(?:'re| are) (?:going|switching|moving|using)|we(?:'ll| will) use|let's use|going with|"
    r"switch(?:ed|ing)? (?:to|from|storage|over)|migrat(?:e|ing) (?:to|from))\b", re.I)
# "instead of" is a decision only when someone is choosing ("we'll keep X instead of Y", "Use X instead of Y"), not in a
# description ("teams call lumen instead of the providers").
INSTEAD_OF = re.compile(r"\binstead of\b", re.I)
CHOOSER = re.compile(r"\b(?:we|us|our|let's|i)\b|^\W*(?:use|go with|pick|choose|prefer|keep|switch)\b", re.I)
CHANGE = re.compile(r"\b(switch|instead|replac|mov(?:e|ed|ing) (?:from|to|away)|no longer|migrat|drop|scratch|ditch|after all)", re.I)
# What a change sentence says it replaces: "from SQLite to …", "instead of SQLite", "replacing SQLite", "dropping SQLite".
REPLACED = re.compile(r"\b(?:from|instead of|replac(?:e|es|ed|ing)|drop(?:s|ped|ping)?|scratch|ditch(?:ing)?)\s+((?:[\w./-]+\s*){1,3})", re.I)
REPLACED_STOP = re.compile(r"\s(?:to|with|for|because|by|and|so|in|on)\b.*", re.I | re.S)
GENERIC = content_words("decision change now for later because we're switching moving instead use using project code "
                        "going with switched replace replacing migrate")
FAILURE_LINE = re.compile(r"error|fail|exception|assert|traceback|panic|not found|denied|refused|"
                          r"^(?:expected|received|actual)\b\s*:", re.I)
NOT_A_FAILURE = re.compile(r"Permission for this action was denied|doesn't want to proceed|was rejected by the user|user denied|"
                           r"classifier gave no verdict", re.I)
# A pipe hides the exit code (`make test | tail`); these lines still say the command failed.
PIPED_FAILURE = re.compile(r"^make(?:\[\d+\])?: \*\*\* .*Error \d+|\b[1-9]\d* (?:failed|errors?)\b|^\S+:\d+(?::\d+)?: (?:fatal )?error\b|^\S+\(\d+,\d+\): error\b|"
                           r"\bFAILED\b|^FAIL\b", re.M)
ECHOED_EXIT = re.compile(r"^(?:exit(?:[ _]?code| status)?|rc|status)\s*[=:]\s*[1-9]\d*\s*$", re.I | re.M)
# Lookups whose non-zero exit means "not found", not a failure (ls of a missing path, grep with no match).
PROBES = {"ls", "test", "[", "grep", "rg", "which", "command", "type", "find", "stat", "cat", "head", "tail", "wc", "file", "echo"}
MAX_PROMPTS = 40
MAX_FAILURES = 30
# Active auto-detected user items before the oldest are archived. High on purpose: the digest ranks them by relevance
# to the current work within its token budget, so a large project's early rules stay available instead of aging out.
MAX_USER_AUTO_ITEMS = 500
# The most recent user prompt at least this long is kept as the defining brief.
BRIEF_MIN_CHARS = 400
# Panel/skill control prompts ("ctx panel", "/ctx reset") are commands, not conversation.
CONTROL = re.compile(r"^/?ctx(\s+[\w-]+)?\s*$", re.I)


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def clean_prompt(content):
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if not parts:
            return None
        content = "\n".join(parts)
    if not isinstance(content, str):
        return None
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", content, flags=re.S)
    text = re.sub(r"</?pasted_content[^>]*>", "", text).strip()
    if not text or text.startswith(SKIP_PREFIXES):
        return None
    return text


def is_probe(cmd):
    parts = [p.split() for p in re.split(r"&&|\|\||[;|\n]", cmd) if p.strip()]
    return bool(parts) and all(p[0] in PROBES or p[0] == "cd" for p in parts)


def sentences(text):
    # Re-join hard-wrapped lines: split into paragraphs / list items first, then sentences.
    for para in re.split(r"\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)]|#+)\s)", text):
        # Markdown emphasis goes; underscores inside identifiers (amount_cents) stay.
        para = re.sub(r"(?<!\w)(_{1,3})(\S(?:.*?\S)?)\1(?!\w)", r"\2", re.sub(r"[`*]{1,3}", "", " ".join(para.split())))
        for sent in re.split(r"(?<=[.!?])\s+", para):
            s = re.sub(r"^\d+[.)]\s*", "", sent.strip(" -•\t#"))
            # Questions and lead-ins ("It must also handle:") are not complete statements.
            if 15 <= len(s) <= 300 and not s.endswith(("?", ":")):
                yield s


def classify(text):
    """Verbatim (kind, sentence) pairs from a user prompt: decisions and requirement-like directives."""
    out = []
    for s in sentences(text):
        task = TASK.search(s)
        if task:
            out.append(("task", s))
        elif DECISION.search(s) or TECH_PLAN.search(s) or INSTEAD_OF.search(s) and CHOOSER.search(s):
            out.append(("decision", s))
        elif TRANSIENT.search(s):
            continue
        elif (DIRECTIVE_START.search(s) or DIRECTIVE_MODAL.search(HEDGED.sub("", s)) or PREFERENCE.search(unquoted(s))
              or UNIVERSAL_RULE.search(s) or PROHIBITION.search(unquoted(s))
              or any(CLAUSE_DIRECTIVE.search(c.strip()) for c in CLAUSE_SPLIT.split(COORD_DESCRIPTIVE.sub("", unquoted(s)))[1:])):
            out.append(("requirement", s))
    return out[:12]


def directives(text):
    return [s for kind, s in classify(text) if kind == "requirement"]


def unquoted(s):
    """A sentence with its quotations removed: quoting a rule ("the header says 'never …'") is not stating one."""
    return re.sub(r'"[^"]*"|“[^”]*”', '""', s)


def user_reported_fixes(st, text):
    """Mark the error lines of open failures that the user says are fixed. Returns the failure IDs touched."""
    touched = []
    pairs = [(c, sent) for sent in re.split(r"(?<=[.!?])\s+", text) for c in FIX_CLAUSES.split(sent) if c]
    for clause, sent in pairs:
        if not FIX_CLAIM.search(clause) or FIX_NEGATED.search(clause) or clause.rstrip().endswith("?"):
            continue
        words = {w for w in re.findall(r"[a-z][a-z0-9]{3,}", clause.lower()) if w not in FIX_STOP}
        # "…was a missing fixture file; Marco fixed that too": the claim points back at the rest of its sentence.
        if re.search(r"\b(?:that|this|it|those|these)(?: one)?\b", clause, re.I) and not FIX_NEGATED.search(sent):
            words |= {w for w in re.findall(r"[a-z][a-z0-9]{3,}", sent.lower()) if w not in FIX_STOP}
        if not words:
            continue
        for f in st["failures"]:
            if f.get("resolved"):
                continue
            lines = f["err"].split(" ⏎ ")
            hit = [l for l in lines
                   if any(t.startswith(w) for w in words for t in re.findall(r"[a-z][a-z0-9]{2,}", l.lower()))]
            if not hit:
                continue
            f.setdefault("fixed_lines", []).extend(hit)
            rest = [l for l in lines if l not in hit]
            f["err"] = " ⏎ ".join(rest)
            # Only a summary line ("2 failed, 518 passed") left means nothing specific is still failing here.
            if not rest:
                f["resolved"] = True
            touched.append(f["id"])
    return touched


def change_targets(st, sentence):
    """Active decisions a 'we switched from X to Y' sentence replaces (shared subject words)."""
    if not CHANGE.search(sentence):
        return None
    # Naming what is replaced narrows the targets to decisions about that thing; otherwise any shared subject word counts
    # (a project name shared by every decision would otherwise let one change replace all of them).
    replaced = set().union(*(content_words(REPLACED_STOP.sub("", m)) for m in REPLACED.findall(sentence))) - GENERIC
    words = replaced or content_words(sentence) - GENERIC
    hits = [it["id"] for it in st["items"]
            if it["status"] == "active" and it["kind"] == "decision" and words & (content_words(it["text"]) - GENERIC)]
    return hits or None


def tool_result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def ingest_transcript(store, st, path, session_id=None):
    """Parse new lines of one transcript into state + history. Returns lines parsed."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    cur = st["cursors"].get(path, 0)
    if cur > size:  # truncated / replaced
        cur = 0
    if cur == size:
        return 0
    sid = session_id or os.path.splitext(os.path.basename(path))[0]
    sess = st["sessions"].setdefault(sid, {"transcript": path})
    pending = st.setdefault("_pending_tools", {})
    n = 0
    with open(path, "rb") as f:
        f.seek(cur)
        data = f.read()
    # Only consume complete lines; a partially-written last line is retried next time.
    end = data.rfind(b"\n") + 1
    for raw in data[:end].splitlines():
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        n += 1
        _ingest_record(store, st, sess, sid, path, pending, o)
    st["cursors"][path] = cur + end
    # Keep only recent pending tool calls (their results may arrive in the next chunk).
    if len(pending) > 200:
        for k in list(pending)[:-200]:
            pending.pop(k, None)
    return n


def _ingest_record(store, st, sess, sid, path, pending, o):
    t = o.get("type")
    ts = parse_ts(o.get("timestamp")) or sess.get("last_ts")
    if o.get("isSidechain"):
        return
    if t == "system" and o.get("subtype") == "compact_boundary":
        meta = o.get("compactMetadata") or {}
        store.metric("compact", session=sid, trigger=meta.get("trigger"), pre=meta.get("preTokens"), post=meta.get("postTokens"))
        return
    msg = o.get("message") or {}
    if t == "user":
        if o.get("isMeta") or o.get("isCompactSummary"):
            return
        content = msg.get("content")
        if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            for b in content:
                if b.get("type") != "tool_result":
                    continue
                call = pending.pop(b.get("tool_use_id"), None)
                if not call:
                    continue
                text = tool_result_text(b)
                if call["name"] == "Bash":
                    _bash_result(store, st, sid, ts, call, text, bool(b.get("is_error")), o.get("uuid"), path)
            return
        text = clean_prompt(content)
        if not text:
            return
        if CONTROL.match(text):
            store.history({"ts": ts, "session": sid, "kind": "prompt", "text": text, "uuid": o.get("uuid"), "transcript": path})
            return
        st["prompts"].append({"ts": ts, "session": sid, "text": text[:4000], "uuid": o.get("uuid")})
        brief = st.get("brief")
        # The defining brief is the longest prompt so far (a long spec outranks later short follow-ups).
        if len(text) >= BRIEF_MIN_CHARS and (not brief or len(text) >= brief["chars"]):
            st["brief"] = {"ts": ts, "session": sid, "text": text[:6000], "uuid": o.get("uuid"), "chars": len(text)}
        del st["prompts"][:-MAX_PROMPTS]
        store.history({"ts": ts, "session": sid, "kind": "prompt", "text": text, "uuid": o.get("uuid"), "transcript": path})
        user_reported_fixes(st, text)
        for kind, sentence in classify(text):
            targets = change_targets(st, sentence) if kind == "decision" else None
            add_item(st, kind, sentence, source="user-auto", supersedes=targets, ts=ts)
        auto = [i for i in st["items"] if i["source"] == "user-auto" and i["status"] == "active"]
        for old in sorted(auto, key=lambda i: i["updated"])[:-MAX_USER_AUTO_ITEMS]:
            old["status"] = "archived"
        return
    if t != "assistant":
        return
    model = msg.get("model")
    usage = msg.get("usage") or {}
    if model and model != "<synthetic>":
        sess["model"] = model
        if usage and o.get("apiBlockIndex", 0) == 0:
            ctx = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            sess["last_ctx"] = ctx
            cc = usage.get("cache_creation") or {}
            if cc.get("ephemeral_1h_input_tokens"):
                sess["ttl"] = 3600
            elif cc.get("ephemeral_5m_input_tokens"):
                sess["ttl"] = 300
    if ts:
        sess["last_ts"] = ts
        sess.setdefault("first_ts", ts)
    texts = []
    for b in msg.get("content") or []:
        bt = b.get("type")
        if bt == "text" and b.get("text", "").strip():
            texts.append(b["text"].strip())
        elif bt == "tool_use":
            _tool_use(store, st, sid, ts, pending, b, o.get("uuid"), path)
    if texts:
        text = "\n".join(texts)
        st["last_status"] = {"ts": ts, "session": sid, "model": sess.get("model"), "text": text[-2000:]}
        store.history({"ts": ts, "session": sid, "kind": "assistant", "text": text, "uuid": o.get("uuid"), "transcript": path})


def _tool_use(store, st, sid, ts, pending, b, uuid, path):
    name, inp = b.get("name", ""), b.get("input") or {}
    if name in EDIT_TOOLS:
        fp = inp.get("file_path") or inp.get("notebook_path")
        if fp:
            f = st["files"].setdefault(fp, {})
            f["edits"] = f.get("edits", 0) + 1
            f["last_edit"] = ts
    elif name == "Read":
        fp = inp.get("file_path")
        if fp:
            f = st["files"].setdefault(fp, {})
            f["reads"] = f.get("reads", 0) + 1
            f["last_read"] = ts
    elif name == "TodoWrite":
        st["todos"] = {"ts": ts, "items": inp.get("todos") or []}
    elif name == "Bash":
        pending[b.get("id")] = {"name": "Bash", "cmd": inp.get("command", ""), "desc": inp.get("description", "")}
        return
    if name in EDIT_TOOLS or name in ("TodoWrite",):
        store.history({"ts": ts, "session": sid, "kind": "tool", "tool": name, "text": json.dumps(inp)[:600], "uuid": uuid, "transcript": path})


def _bash_result(store, st, sid, ts, call, text, is_error, uuid, path):
    cmd = toolout.strip_wrap(call["cmd"])
    # `make > log 2>&1; echo "exit=$?"; grep error log` exits 0 but prints the real status.
    is_error = is_error or bool(toolout.EXIT_LINE.search(text)) or "$?" in cmd and bool(ECHOED_EXIT.search(text))
    store.history({
        "ts": ts, "session": sid, "kind": "tool", "tool": "Bash",
        "text": f"$ {cmd}\n{text[:1500]}", "error": is_error, "uuid": uuid, "transcript": path,
    })
    if is_error and not NOT_A_FAILURE.search(text) and not is_probe(cmd) or not is_error and piped_failure(cmd, text):
        record_failure(st, ts, cmd, text)
    elif not read_failure_log(st, cmd, text):
        ok = cmd_signature(cmd)
        for f in st["failures"]:
            if f.get("resolved"):
                continue
            failed = cmd_signature(f["cmd"])
            # Same command, or a broader run of it (`npm test` passing resolves `npm test -- a.spec`); a bare
            # program name (`python3 -V`) is too broad to count.
            if f["cmd"] == cmd[:300] or (len(ok) >= 2 and failed[:len(ok)] == ok):
                f["resolved"] = True


def log_files(cmd):
    """Files a command's output is redirected into (`make test > build.log 2>&1` → build.log)."""
    return set(re.findall(r"(?<!&)\d?>>?\s*([^\s&;|<>][^\s;|<>]*)", cmd)) - {"/dev/null"}


def read_failure_log(st, cmd, text):
    """A later read of a failed run's log (`sed -n 60,90p build.log`, `L=build.log; grep FAIL $L`) holds the error
    detail the run itself hid; it is added to that failure. True when the command was such a read."""
    expanded = cmd
    for name, value in re.findall(r"\b([A-Za-z_]\w*)=(\S+)", cmd):
        # A function replacement, so a value with backslashes (`P=C:\x`) is not read as a template.
        expanded = re.sub(r"\$\{?" + name + r"\b\}?", lambda _m, v=value: v, expanded)
    words = set(re.split(r"[\s;|&'\"]+", expanded))
    for f in reversed(st["failures"]):
        if not f.get("resolved") and words & log_files(f["cmd"]):
            new = [l for l in failure_lines(text).split(" ⏎ ") if FAILURE_LINE.search(l)]
            old = f["err"].split(" ⏎ ")
            f["err"] = " ⏎ ".join((old + [l for l in new if l not in old])[-12:])
            return True
    return False


def pipeline_head(cmd):
    """The command a pipeline runs, before its first `|` (`cd app && make test 2>&1 | tail` → `cd app && make test 2>&1`)."""
    return re.split(r"(?<!\|)\|(?!\|)", cmd)[0]


def piped_failure(cmd, text):
    if pipeline_head(cmd) == cmd:
        return False
    parts = [p.split() for p in re.split(r"&&|;|\n", pipeline_head(cmd)) if p.strip() and p.split()[0] != "cd"]
    return bool(parts) and parts[-1][0] not in PROBES and bool(PIPED_FAILURE.search(text))


def record_failure(st, ts, cmd, text, limit=12):
    """A new failure, or more detail on the open failure of the same command (`make build` then `make build | grep error`:
    the transcript keeps only the head of a long failing log, the follow-up has the actual error lines)."""
    new = failure_lines(text).split(" ⏎ ")
    base = cmd_signature(pipeline_head(cmd))
    for f in reversed(st["failures"]):
        if f.get("resolved") or not base or cmd_signature(pipeline_head(f["cmd"])) != base:
            continue
        old = f["err"].split(" ⏎ ") if FAILURE_LINE.search(f["err"]) else []  # a log head with no error line in it is dropped
        if any(FAILURE_LINE.search(l) for l in new):
            f["err"] = " ⏎ ".join((old + [l for l in new if l not in old])[-limit:])
        return
    st["failures"].append({"id": next_failure_id(st), "ts": ts, "cmd": cmd[:300], "err": " ⏎ ".join(new), "resolved": False})
    del st["failures"][:-MAX_FAILURES]


def cmd_signature(cmd, max_words=4):
    """Program and positional args of the last non-`cd` segment, flags and env assignments dropped."""
    segs = [p.split() for p in re.split(r"&&|\|\||;|\n", cmd) if p.strip()]
    segs = [p for p in segs if p[0] != "cd"]
    if not segs:
        return ()
    words = [w for w in segs[-1] if not w.startswith("-") and not re.match(r"^[A-Za-z_]\w*=", w)]
    words = [w for w in words if w not in ("2>&1", "|")]
    return tuple(words[:max_words])


def failure_lines(text, limit=10):
    """Distinct error lines of a failed command (a test run can fail in several places)."""
    seen = []
    for line in text.splitlines():
        l = re.sub(r"^\d+: ", "", line.strip())
        if not l or l.startswith(("[ctxlc]", "---", "…")) or l in seen:
            continue
        if FAILURE_LINE.search(l):
            seen.append(l[:200])
            if len(seen) >= limit:
                break
    return " ⏎ ".join(seen) if seen else text.strip()[:200]


def transcripts_for_project(project):
    """Claude Code stores transcripts under ~/.claude/projects/<cwd with non-alnum → '-'>/."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(project))
    d = os.path.join(os.path.expanduser("~/.claude/projects"), slug)
    try:
        return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl"))
    except OSError:
        return []


def ingest_all(store, st, extra=None):
    paths = transcripts_for_project(store.project)
    if extra and extra not in paths:
        paths.append(extra)
    return sum(ingest_transcript(store, st, p) for p in paths)
