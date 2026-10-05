"""L2 durable project state + L3 history files, with file locking.

Layout (per project):
  .claude/context/
    state.json        structured state (items, files, todos, prompts, sessions)
    history.jsonl     L3: append-only prompts / assistant text / tool calls / compact summaries
    artifacts/        L3: full copies of tool outputs that were compacted
    epochs/           per-context-window dedup memory (reset on compact/clear)
    metrics.jsonl     token accounting events
    config.json       optional overrides of config.DEFAULTS
"""

import contextlib
import json
import math
import os
import re
import time

from . import tokens

try:
    import fcntl
    msvcrt = None
except ImportError:  # Windows
    fcntl = None
    import msvcrt

STATE_VERSION = 1

KIND_PREFIX = {
    "requirement": "R",
    "constraint": "C",
    "decision": "D",
    "task": "T",
    "bug": "B",
    "issue": "I",
    "config": "K",
    "note": "N",
}
# Kinds where a new, similar item replaces an older active one.
SUPERSEDABLE = {"decision", "constraint", "config", "requirement", "note"}
SUPERSEDE_JACCARD = 0.5
# Kinds where similar wording about two different subjects ("for billing-api, …" / "for auth-api, …") is two items.
# Decisions are left out: "use X for the ledger" after "use Y for the ledger" is a replacement.
SUBJECT_GUARDED = {"constraint", "config", "requirement", "note"}
# Words a restatement swaps freely; changing them does not change the subject.
INTERCHANGEABLE = set("must should shall need needs has have always every all each any never ensure".split())
VALUE = re.compile(r"[\d.,:]+[a-z%]{0,4}")

STOPWORDS = set(
    "a an the and or but if then else of to in on at for with by from as is are was were be been "
    "it its this that these those we you i he she they them our your use using used not no do does "
    "did should must will would can could may might have has had so than too very just only also into "
    "over under about after before when while which who what where how all any each more most other "
    "some such own same".split()
)


# A Cowork session runs Claude Code on the host with cwd <session>/outputs; that folder is shown to the user and
# mounted into the VM, so the store goes in the session directory beside it.
COWORK_OUTPUTS = re.compile(r"(.*[/\\]local-agent-mode-sessions[/\\].+[/\\]local_[^/\\]+)[/\\]outputs[/\\]?$")


def cowork_session_dir(path):
    m = COWORK_OUTPUTS.match(path or "")
    return m.group(1) if m else None


def is_cowork(cwd=None):
    """Cowork (the desktop app's agent mode): the model's shell runs in a VM that cannot reach host paths, so
    nothing injected may ask it to run `ctx`. Read/Edit/Write still run on the host."""
    return bool(os.environ.get("CLAUDE_CODE_IS_COWORK") or cowork_session_dir(os.path.abspath(cwd or os.getcwd())))


def project_dir(cwd=None):
    env = os.environ.get("CTXLC_PROJECT") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return cowork_session_dir(os.path.abspath(env)) or os.path.abspath(env)
    start = os.path.abspath(cwd or os.getcwd())
    if cowork_session_dir(start):
        return cowork_session_dir(start)
    d = start
    while True:
        if os.path.isdir(os.path.join(d, ".claude", "context")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return start
        d = parent


def content_words(text):
    # Sentence punctuation is not part of a word ("…in Redis." names redis), path separators inside one are.
    words = (w.strip("./-") for w in re.findall(r"[a-z0-9_./-]{2,}", text.lower()))
    return {w for w in words if len(w) >= 2 and w not in STOPWORDS}


def jaccard(a, b):
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def different_subjects(a, b):
    """True when each text names something the other does not, beyond values and modal wording.

    A restatement adds words ("…to clients, including 500s") or changes a value ("30 seconds" to "10 seconds").
    Templated rules about different services, endpoints or files differ in a name on both sides."""
    def norm(ws):
        return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in ws}
    wa, wb = norm(content_words(a)), norm(content_words(b))

    def names(ws):
        return {w for w in ws if w not in INTERCHANGEABLE and not VALUE.fullmatch(w)}
    return bool(names(wa - wb)) and bool(names(wb - wa))


def focus_text(st, recent_prompts=6):
    """What the session is working on now: the current task, the last few user messages and the last status."""
    parts = [it["text"] for it in st["items"] if it["status"] == "active" and it["kind"] == "task"][-1:]
    parts += [p["text"] for p in st.get("prompts", [])[-recent_prompts:]]
    if st.get("last_status"):
        parts.append(st["last_status"]["text"])
    return " ".join(parts)


def by_relevance(items, focus):
    """Order items so the ones that share rare words with the current work come first, keeping the given order otherwise.

    Words are weighted by how few items use them, so a module name mentioned in the last prompt outweighs
    wording that every templated rule shares. Large projects accumulate hundreds of rules; this is what keeps
    an old but relevant one inside the budget instead of the newest ones."""
    fw = content_words(focus)
    if not fw or len(items) < 2:
        return list(items)
    words = [content_words(it["text"]) for it in items]
    df = {}
    for ws in words:
        for w in ws & fw:
            df[w] = df.get(w, 0) + 1
    n = len(items)
    score = [sum(math.log((n + 1) / df[w]) for w in ws & fw) for ws in words]
    order = sorted(range(n), key=lambda k: (-score[k], k))
    return [items[k] for k in order]


def now():
    return time.time()


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def empty_state(project):
    return {
        "version": STATE_VERSION,
        "project": project,
        "items": [],
        "next_id": {},
        "files": {},
        "prompts": [],
        "todos": None,
        "failures": [],
        "last_status": None,
        "last_compact_summary": None,
        "sessions": {},
        "cursors": {},
    }


def _replace(src, dst):
    """os.replace, retried on Windows, where it fails while another process (a hook reading state) has dst open."""
    for attempt in range(50):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if fcntl or attempt == 49:
                raise
            time.sleep(0.02)


class Store:
    def __init__(self, project=None):
        self.project = project or project_dir()
        self.dir = os.path.join(self.project, ".claude", "context")
        self.state_path = os.path.join(self.dir, "state.json")
        self.history_path = os.path.join(self.dir, "history.jsonl")
        self.metrics_path = os.path.join(self.dir, "metrics.jsonl")
        self.artifacts = os.path.join(self.dir, "artifacts")
        self.epochs = os.path.join(self.dir, "epochs")
        # One marker per hook event handled, so a second copy of ctxlc's hooks can tell it is a duplicate.
        self.runs = os.path.join(self.dir, "runs")

    def ensure(self):
        for d in (self.dir, self.artifacts, self.epochs):
            os.makedirs(d, mode=0o700, exist_ok=True)
        # Archived outputs and history can hold secrets the session printed: owner-only.
        if fcntl and os.stat(self.dir).st_mode & 0o077:  # Windows has no POSIX modes; the profile folder is private
            os.chmod(self.dir, 0o700)
        gi = os.path.join(self.dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write("*\n")

    # --- locking -------------------------------------------------------------
    @contextlib.contextmanager
    def locked(self):
        self.ensure()
        with open(os.path.join(self.dir, ".lock"), "a+", encoding="utf-8") as lf:
            if fcntl:
                fcntl.flock(lf, fcntl.LOCK_EX)
            else:
                lf.seek(0)
                while True:
                    try:
                        msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:  # LK_LOCK gives up after ~10 s; keep waiting, as flock does
                        pass
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                else:
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)

    def load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("version") == STATE_VERSION:
                return st
        except (OSError, ValueError):
            pass
        return empty_state(self.project)

    def save(self, st):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        _replace(tmp, self.state_path)

    @contextlib.contextmanager
    def transaction(self):
        with self.locked():
            st = self.load()
            yield st
            self.save(st)

    # --- append-only logs -------------------------------------------------------
    def append(self, path, rec):
        self.ensure()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def history(self, rec):
        self.append(self.history_path, rec)

    def metric(self, event, **kw):
        kw.update(event=event, ts=now())
        self.append(self.metrics_path, kw)

    def iter_jsonl(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            return

    # --- epoch (per context window) memory for dedup ------------------------------
    def epoch_path(self, key):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
        return os.path.join(self.epochs, safe + ".json")

    def load_epoch(self, key):
        try:
            with open(self.epoch_path(key), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"outputs": {}, "reads": {}, "started": now()}

    def save_epoch(self, key, ep):
        self.ensure()
        with open(self.epoch_path(key), "w", encoding="utf-8") as f:
            json.dump(ep, f)

    def reset_epoch(self, key):
        try:
            os.remove(self.epoch_path(key))
        except OSError:
            pass

    # --- retention ------------------------------------------------------------------
    def gc(self, artifact_max_age_days=14, history_max_bytes=20_000_000):
        """Drop old archived outputs / epochs and keep the newest half of an oversized history.

        Returns (files removed, history bytes dropped). State items are never touched.
        """
        cutoff = time.time() - artifact_max_age_days * 86400
        removed = 0
        for d in (self.artifacts, self.epochs):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                p = os.path.join(d, n)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
        try:
            names = os.listdir(self.runs)
        except OSError:
            names = []
        for n in names:  # only needed for seconds; keep an hour
            p = os.path.join(self.runs, n)
            try:
                if os.path.getmtime(p) < time.time() - 3600:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        dropped = 0
        try:
            size = os.path.getsize(self.history_path)
        except OSError:
            size = 0
        if size > history_max_bytes:
            with self.locked():
                with open(self.history_path, "rb") as f:
                    f.seek(size - history_max_bytes // 2)
                    f.readline()  # skip the partial line
                    keep = f.read()
                tmp = self.history_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(keep)
                _replace(tmp, self.history_path)
                dropped = size - len(keep)
        return removed, dropped


# --- items ------------------------------------------------------------------------
def add_item(st, kind, text, source="agent", supersedes=None, ts=None):
    """Add a state item. Returns (item, [superseded ids]).

    Explicit `supersedes` wins. Otherwise an active item of the same kind that is
    near-identical in content is superseded automatically; a new `task` always
    replaces the previous active task (there is one current task).
    """
    ts = ts or now()
    text = text.strip()
    for it in st["items"]:
        if it["status"] == "active" and it["kind"] == kind and it["text"] == text:
            it["updated"] = ts
            return it, []
    prefix = KIND_PREFIX.get(kind, "N")
    n = st["next_id"].get(prefix, 0) + 1
    st["next_id"][prefix] = n
    item = {
        "id": f"{prefix}{n}",
        "kind": kind,
        "text": text,
        "status": "active",
        "source": source,
        "created": ts,
        "updated": ts,
    }
    replaced = []
    targets = set(supersedes or [])
    for it in st["items"]:
        if it["status"] != "active":
            continue
        if it["id"] in targets:
            replaced.append(it)
        elif kind == "task" and it["kind"] == "task":
            replaced.append(it)
        elif not targets and kind in SUPERSEDABLE and it["kind"] == kind and jaccard(it["text"], text) >= SUPERSEDE_JACCARD \
                and not (kind in SUBJECT_GUARDED and different_subjects(it["text"], text)):
            replaced.append(it)
    for it in replaced:
        it["status"] = "superseded" if it["kind"] != "task" else "done"
        it["superseded_by"] = item["id"]
        it["updated"] = ts
    if replaced:
        item["supersedes"] = [it["id"] for it in replaced]
    st["items"].append(item)
    return item, [it["id"] for it in replaced]


def next_failure_id(st):
    n = st.get("next_failure", 1)
    st["next_failure"] = n + 1
    return f"F{n}"


def _failure_ids(st):
    # Failures recorded before IDs existed get one on first use.
    for f in st.get("failures", []):
        if "id" not in f:
            f["id"] = next_failure_id(st)


def set_status(st, item_id, status, ts=None):
    for it in st["items"]:
        if it["id"].lower() == item_id.lower():
            it["status"] = status
            it["updated"] = ts or now()
            return it
    _failure_ids(st)
    for f in st.get("failures", []):
        if f["id"].lower() == item_id.lower():
            f["resolved"] = status != "active"
            return f
    return None


# --- digest rendering ---------------------------------------------------------------
def _clip(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def handoff_live(ho, session=None, ttl_s=6 * 3600, now=None):
    """A handoff is for the next context, not every future one.

    It is shown until a session consumes it, then only to that session (so it survives
    that session's compactions) or for ttl_s after consumption (a quick second restart).
    """
    if not ho:
        return False
    if not ho.get("consumed_by") or ho["consumed_by"] == session:
        return True
    return (now or time.time()) - ho.get("consumed_at", 0) < ttl_s


def render_digest(st, budget_tokens=2500, recent_prompts=6, cli="ctx", session=None, handoff_ttl_s=6 * 3600,
                  state_path=None, max_chars=None):
    """Render the smallest sufficient state for continuing work.

    Sections are emitted in priority order; lower-priority sections are dropped
    (with a pointer) once the token budget is used, so explicit requirements,
    constraints and the current task are never the ones that get cut.

    With state_path and no cli (Cowork, where the shell cannot run `ctx`), pointers name the state file instead.
    max_chars is a hard cap next to the token budget: Claude Code replaces hook context over 10,000 chars with a
    2 KB preview, so a digest that is too long loses everything after its first lines.
    """
    active = [it for it in st["items"] if it["status"] == "active"]
    by_kind = {}
    for it in sorted(active, key=lambda i: i["updated"], reverse=True):
        by_kind.setdefault(it["kind"], []).append(it)

    sections = []

    def sec(title, lines):
        if lines:
            sections.append((title, lines))

    task = by_kind.get("task", [])
    todos = (st.get("todos") or {}).get("items") or []
    open_todos = [t for t in todos if t.get("status") != "completed"]
    lines = [f"[{t['id']}] {t['text']}" for t in task[:1]]
    lines += [f"({t.get('status', '?')}) {_clip(t.get('content', ''), 160)}" for t in open_todos[:12]]
    if todos and not open_todos:
        lines.append(f"(all {len(todos)} todos completed)")
    sec("Current task", lines)

    ho = st.get("handoff")
    if handoff_live(ho, session, handoff_ttl_s):
        # One bullet per line the previous context wrote; 2,000 chars in total.
        lines, left = [], 2000
        for ln in ho["text"].splitlines():
            ln = _clip(ln.strip().lstrip("-*• ").strip(), left)
            if ln and left > 0:
                lines.append(ln)
                left -= len(ln)
        sec(f"Handoff written by the previous context ({fmt_ts(ho['ts'])}, {ho.get('model') or '?'})", lines)

    brief = st.get("brief")
    if brief:
        more = f" … [{brief['chars']:,} chars total; full text: `{cli} show u:{brief['uuid']}`]" if brief["chars"] > 1400 else ""
        sec(f"Defining brief from the user ({fmt_ts(brief['ts'])})", [_clip(brief["text"], 1400) + more])

    req =by_kind.get("requirement", []) + by_kind.get("constraint", [])
    req.sort(key=lambda i: (i["source"] == "user-auto", -i["updated"]))
    focus = focus_text(st, recent_prompts)
    req = by_relevance(req, focus)
    sec("Requirements & constraints (active)", [f"[{i['id']}] {_clip(i['text'], 300)}" for i in req])
    by_id = {it["id"]: it for it in st["items"]}

    def replaces(i):
        old = [by_id[x] for x in i.get("supersedes", []) if x in by_id]
        return "".join(f" (replaces {o['id']}: {_clip(o['text'], 90)})" for o in old)

    sec("Decisions (active)", [f"[{i['id']}] {_clip(i['text'], 260)}{replaces(i)}" for i in by_relevance(by_kind.get("decision", []), focus)])

    probs = [f"[{i['id']}] {_clip(i['text'], 220)}" for i in by_kind.get("bug", []) + by_kind.get("issue", [])]
    _failure_ids(st)
    fails = [f for f in st.get("failures", []) if not f.get("resolved")][-5:]
    probs += [f"[{f['id']}] (failed {fmt_ts(f['ts'])}) `{_clip(f['cmd'], 120)}` → {_clip(f['err'], 600)}"
              + (f" — the user reported fixed since: {_clip(' ⏎ '.join(f['fixed_lines']), 300)}" if f.get("fixed_lines") else "")
              for f in fails]
    sec("Open bugs / unresolved issues", probs)
    sec("Config & environment", [f"[{i['id']}] {_clip(i['text'], 220)}" for i in by_relevance(by_kind.get("config", []), focus)])
    sec("Notes", [f"[{i['id']}] {_clip(i['text'], 220)}" for i in by_relevance(by_kind.get("note", []), focus)])

    ls = st.get("last_status")
    if ls:
        sec(f"Last assistant status ({fmt_ts(ls['ts'])}, {ls.get('model', '?')})", [_clip(ls["text"], 900)])

    prompts = [p for p in st.get("prompts", [])[-recent_prompts:] if not brief or p.get("uuid") != brief.get("uuid")]
    sec("Recent user messages (oldest first)", [f"{fmt_ts(p['ts'])}: {_clip(p['text'], 320)}" for p in prompts])

    files = sorted(st.get("files", {}).items(), key=lambda kv: kv[1].get("last_edit") or 0, reverse=True)
    edited = [f"{path} ({v.get('edits', 0)} edits, {fmt_ts(v.get('last_edit'))})" for path, v in files if v.get("edits")]
    sec("Recently modified files (source of truth — re-read, don't trust memory)", edited[:12])

    superseded = sum(1 for it in st["items"] if it["status"] == "superseded")
    if cli:
        lookup = (f"Older raw history is NOT in context but is searchable: "
                  f"`{cli} search \"<terms>\"`, full detail: `{cli} show <ref>`. "
                  f"{superseded} superseded item(s) hidden (`{cli} state --all`).")
        more = f"`{cli} state`"
    else:
        lookup = (f"Older raw history is NOT in context. Every item, including any cut below for space, is in "
                  f"`{state_path}` (open it with the Read tool; the shell cannot reach it).")
        more = "full list in the state file"
    header = [
        f"# Project context (ctxlc) — {st.get('project')}",
        "Reference state restored from earlier work in this project. It is background, not a request: respond to the "
        "user's next message, and do not resume earlier tasks unless the user asks you to continue. " + lookup,
    ]
    out = list(header)
    used = tokens.estimate("\n".join(out))
    # Room for the "… N more" and "(omitted …)" pointer lines.
    char_room = (max_chars - 300 if max_chars else float("inf")) - len("\n".join(out))
    dropped = []
    for title, lines in sections:
        block = [f"\n## {title}"] + [f"- {l}" for l in lines]
        text = "\n".join(block)
        cost = tokens.estimate(text)
        if used + cost <= budget_tokens and len(text) + 1 <= char_room:
            out += block
            used += cost
            char_room -= len(text) + 1
            continue
        # Partially fit high-priority sections line by line instead of dropping them.
        kept = [block[0]]
        char_room -= len(block[0]) + 1
        for l in block[1:]:
            c = tokens.estimate(l)
            if used + c > budget_tokens or len(l) + 1 > char_room:
                break
            kept.append(l)
            used += c
            char_room -= len(l) + 1
        if len(kept) > 1:
            out += kept + [f"- … {len(block) - len(kept)} more ({more})"]
        else:
            char_room += len(block[0]) + 1
            dropped.append(title)
    if dropped:
        out.append(f"\n(omitted for budget: {', '.join(dropped)} — {more})")
    return "\n".join(out)


AGENT_PROTOCOL = """## Context protocol (ctxlc)
This project keeps durable state so long sessions can be compacted/cleared safely. When one of these is established, record it in one short line (cheap; don't narrate):
- `{cli} note requirement|constraint|decision|bug|issue|config "<text>" [--supersedes ID]`
- `{cli} task "<current task>"`   ·   `{cli} resolve <ID>` when a bug/issue is fixed
Bulky tool output may arrive condensed by ctxlc; its header says exactly what is complete. Trust it, and grep the full copy only for something it says is not shown."""

# Cowork: requirements, decisions, the task and failures are read from the transcript; the VM shell cannot run `ctx`.
COWORK_PROTOCOL = """## Context protocol (ctxlc)
This session's requirements, decisions, current task and failures are recorded automatically from the conversation \
and restored after compaction. When you settle a decision or the task changes, say it in one plain sentence so it is \
recorded."""
