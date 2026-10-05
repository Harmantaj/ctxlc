"""L1: rewrite bulky or duplicate tool output before the model sees it.

Every token that enters the conversation is re-read (as cache reads) on every
later request until the next compaction, so trimming at ingestion time is the
cheapest place to save. Nothing is lost: the full output is archived under
.claude/context/artifacts/ and the extract names that path.
"""

import hashlib
import os
import re
import shlex
import time

from . import tokens

SIGNAL = re.compile(
    r"(error|errno|exception|traceback|fatal|fail(ed|ure)?|panic|assert|warn(ing)?|denied|refused|"
    r"not found|cannot|can't|unable|undefined|segfault|timed? ?out|killed|exit (code|status)|✗|✘)",
    re.I,
)
SUMMARY = re.compile(
    r"(\b\d+ (passed|failed|errors?|skipped|tests?|warnings?)\b|^ran \d+ tests?|^ok\b|^FAILED|^PASSED|"
    r"^tests?:|^test result|^={3,}.*={3,}$|\btotal\b|\bsummary\b|\bcompiled\b|\bbuilt\b|\bdone\b|"
    r"added \d+ packages?|up to date|vulnerabilit)",
    re.I,
)
# ctxlc's own CLI output (digest, state, search, show) is already budgeted; condensing it defeats its purpose.
PASSTHROUGH = re.compile(r"#\s*ctx:full|CTX_RAW=1|bin/ctx[\"']?(?:\s|$)|-m ctxlc\b")

# Build/test/install commands are the big, often-failing outputs. A failing command fires
# PostToolUseFailure, whose output cannot be rewritten, and Claude Code then shows the model
# only the first ~10k chars — usually missing the actual errors. For these families only,
# PreToolUse appends a suffix that reports the exit code as text and exits 0, so the
# output reaches PostToolUse and gets an error-aware extract.
NOISY = re.compile(
    r"(^|&&|;|\|\|)\s*(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:time\s+)?"
    r"(pytest|python3? -m (?:pytest|unittest|pip|build)|pip3? install|uv (?:sync|pip|run pytest)|poetry (?:install|run pytest)|"
    r"npm (?:test|t|run|install|i|ci)\b|yarn\b|pnpm\b|npx (?:jest|vitest|tsc|eslint|playwright)|bun (?:test|install|run)|"
    r"make\b|cmake\b|ninja\b|cargo (?:build|test|check|clippy)|go (?:test|build|vet)|mvn\b|gradle\b|\./gradlew\b|"
    r"tsc\b|eslint\b|jest\b|vitest\b|docker (?:build|compose)|xcodebuild\b|swift (?:build|test)|"
    r"bundle (?:exec|install)|rake\b|mix (?:test|compile)|dotnet (?:build|test|restore)|flutter (?:test|build))",
    re.M,
)
# Commands whose output is content the model asked to see (files, search hits, diffs), not a log.
# An error-pattern extract of source code is useless and forces a second, more expensive read,
# so these pass through unchanged up to viewer_passthrough_max_chars.
VIEWERS = {"cat", "head", "tail", "sed", "nl", "bat", "less", "more", "grep", "egrep", "rg", "ag", "awk", "jq", "cut", "column"}
VIEWER_GIT = {"show", "diff", "log", "blame"}
NEUTRAL = {"cd", "echo", "printf", "true", "pwd", "ls", "wc", "sort", "uniq"}
EXIT_MARK = "[ctxlc] exit code"
# The mark as the wrapper prints it: a whole line ending in the code (not the literal quoted in source/docs).
EXIT_LINE = re.compile(r"^" + re.escape(EXIT_MARK) + r" \d+$", re.M)
PERSISTED_READ_MAX = 20_000_000
# Cowork's shell tools are MCP tools, not Bash: device_bash (cloud tasks, runs on the user's computer) and
# mcp__workspace__bash (desktop Cowork VM). Both take {"command": ...} and return text content.
SHELL_MCP = re.compile(r"^mcp__.+__(?:device_bash|bash)$")
WRAP_SUFFIX = f'\n__ctxlc_rc=$?; [ "$__ctxlc_rc" -ne 0 ] && echo "{EXIT_MARK} $__ctxlc_rc"; true'


def wrap_command(tool_input):
    """Return an updated Bash input for noisy commands, or None to leave it alone."""
    cmd = tool_input.get("command", "")
    if (not cmd or tool_input.get("run_in_background") or WRAP_SUFFIX in cmd or PASSTHROUGH.search(cmd)
            or not NOISY.search(cmd)):
        return None
    return dict(tool_input, command=cmd.rstrip() + WRAP_SUFFIX)


def strip_wrap(cmd):
    return cmd.replace(WRAP_SUFFIX, "")


def is_viewer(cmd):
    """True if every segment of the command only displays files/search results (quote-aware)."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens_ = list(lex)
    except ValueError:
        return False
    segments, cur = [], []
    for t in tokens_:
        if t and set(t) <= set("|&;\n"):
            segments.append(cur)
            cur = []
        else:
            cur.append(t)
    segments.append(cur)
    seen_viewer = False
    for seg in segments:
        words = [w for w in seg if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog in VIEWERS or (prog == "git" and len(words) > 1 and words[1] in VIEWER_GIT):
            seen_viewer = True
        elif prog not in NEUTRAL:
            return False
    return seen_viewer


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def fold_repeats(lines):
    """Collapse runs of identical (or number-only-different) lines: 'x' ×37."""
    out, prev_key, count = [], None, 0

    def flush():
        if prev_key is not None:
            out.append(prev_line if count == 1 else f"{prev_line}  [×{count} similar lines]")

    prev_line = None
    for line in lines:
        key = re.sub(r"\d+", "#", line.strip())
        if key == prev_key:
            count += 1
            continue
        flush()
        prev_key, prev_line, count = key, line, 1
    flush()
    return out


def _clip_line(l, n=400):
    return l if len(l) <= n else l[:n] + f"… [+{len(l) - n} chars]"


def extract_diff(text, budget):
    files, cur, hunks = [], None, 0
    for line in text.splitlines():
        if line.startswith("diff --git"):
            cur = {"name": line.split(" b/", 1)[-1], "add": 0, "del": 0, "hunks": []}
            files.append(cur)
        elif cur is not None:
            if line.startswith("@@"):
                cur["hunks"].append(line[:160])
                hunks += 1
            elif line.startswith("+") and not line.startswith("+++"):
                cur["add"] += 1
            elif line.startswith("-") and not line.startswith("---"):
                cur["del"] += 1
    out = [f"git diff: {len(files)} file(s), {hunks} hunk(s) — per-file +/- counts and hunk headers only; diff bodies are NOT shown"]
    for f in files:
        out.append(f"  {f['name']}  +{f['add']} -{f['del']}")
        for h in f["hunks"][:6]:
            out.append(f"    {h}")
        if len(f["hunks"]) > 6:
            out.append(f"    … {len(f['hunks']) - 6} more hunks")
    return "\n".join(out)[:budget]


def extract(text, command="", budget=2500):
    """Deterministic extract: signal lines with context, summary lines, head and tail."""
    lines = text.splitlines()
    if re.search(r"\bgit\b.*\b(diff|show|log -p)\b", command) and "diff --git" in text:
        return extract_diff(text, budget), "PARTIAL: to see a file's changes, run git diff on that path (or Read a line range of the full output)."
    folded = fold_repeats(lines)
    picked = {}
    for i, l in enumerate(folded):
        if SIGNAL.search(l):
            for j in range(max(0, i - 1), min(len(folded), i + 3)):
                picked[j] = folded[j]
        elif SUMMARY.search(l.strip()):
            picked[i] = folded[i]
    head = folded[:12]
    tail = folded[-25:] if len(folded) > 37 else []
    parts = ["--- head ---"] + [_clip_line(l) for l in head]
    sig = [(i, l) for i, l in sorted(picked.items()) if 12 <= i < len(folded) - len(tail)]
    if sig:
        parts.append(f"--- {len(sig)} error/warning/summary lines (with context) ---")
        last = None
        for i, l in sig:
            if last is not None and i != last + 1:
                parts.append("  …")
            parts.append(f"{i + 1}: {_clip_line(l)}")
            last = i
    if tail:
        parts.append("--- tail ---")
        parts += [_clip_line(l) for l in tail]
    out = "\n".join(parts)
    omitted = len(folded) - len(head) - len(sig) - len(tail)
    # Tell the model exactly what is guaranteed complete, so it doesn't re-read the whole file "to be sure".
    if len(out) > budget:
        # Keep the head of the signal section and the whole tail: the end of logs matters most.
        keep_tail = "\n".join(["--- tail ---"] + tail[-15:])
        out = out[: max(200, budget - len(keep_tail) - 40)] + "\n  … [extract truncated]\n" + keep_tail
        note = "INCOMPLETE: the extract hit its size budget, so some error/warning lines are NOT shown — grep the full output for them."
    elif omitted <= 0:
        note = "COMPLETE: every distinct line is shown; runs of near-identical lines are collapsed as [×N]."
    else:
        note = (f"COMPLETE for errors: first {len(head)} and last {len(tail)} lines plus EVERY line matching "
                f"error/warning/failure/summary patterns (with context) are shown; the other {omitted:,} lines matched none of those patterns.")
    return out, note


def archive(store, text, label):
    store.ensure()
    h = digest(text)
    path = os.path.join(store.artifacts, f"{h}.log")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {label}\n# archived {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(text)
    else:
        os.utime(path)  # still referenced: keep it out of retention GC
    return h, path


def compact_bash(store, cfg, epoch, tool_input, response, call_id=None):
    """Return (new_response or None, metric dict or None)."""
    cmd = strip_wrap(tool_input.get("command", ""))
    if PASSTHROUGH.search(cmd) or store.artifacts in cmd or ".claude/context/artifacts" in cmd:
        return None, None
    if response.get("isImage"):
        return None, None
    out, err = response.get("stdout") or "", response.get("stderr") or ""
    persisted = response.get("persistedOutputPath")
    if persisted:
        # Output above bashOutputMaxChars: stdout holds only the head; the full text is on disk.
        try:
            with open(persisted, errors="replace", encoding="utf-8") as f:
                out = f.read(PERSISTED_READ_MAX)
        except OSError:
            persisted = None
    combined = out + ("\n[stderr]\n" + err if err else "")
    size = (response.get("persistedOutputSize") or len(combined)) if persisted else len(combined)
    if size < cfg["dedup_min_chars"]:
        return None, None
    h = digest(combined)
    seen = epoch["outputs"].get(h)
    # Claude Code can run one hook twice for a single call; never match our own record.
    if seen and not (call_id and seen.get("id") == call_id):
        note = (
            f"[ctxlc] Output identical ({size:,} chars) to an earlier command in this context "
            f"(`{seen['cmd'][:120]}` at {seen['at']}); not repeated. Full copy: {seen.get('path') or 'see earlier result above'}"
        )
        new = _replace(response, note)
        return new, {"kind": "bash_dedup", "raw_chars": size, "new_chars": len(note)}
    entry = {"cmd": cmd, "at": time.strftime("%H:%M:%S"), "id": call_id}
    epoch["outputs"][h] = entry
    if size < cfg["bash_compact_min_chars"]:
        return None, None
    if not persisted and size <= cfg["viewer_passthrough_max_chars"] and is_viewer(cmd):
        return None, None
    path = persisted or archive(store, combined, f"$ {cmd}")[1]
    entry["path"] = path
    body, completeness = extract(combined, cmd, cfg["bash_extract_max_chars"])
    n_lines = combined.count("\n") + 1
    header = (
        f"[ctxlc] {size:,} chars / {n_lines:,} lines of output condensed. {completeness} "
        f"Full text, only if you need something not covered: {path} (grep it or Read a line range — don't read it whole)."
    )
    note = header + "\n" + body
    new = _replace(response, note)
    return new, {"kind": "bash_compact", "raw_chars": size, "new_chars": len(note), "raw_tokens": tokens.estimate(combined), "new_tokens": tokens.estimate(note)}


def _replace(response, text):
    # Drop the persisted-output fields: otherwise Claude Code renders its own head preview
    # instead of our text. The extract header already names the full-output path.
    new = {k: v for k, v in response.items() if not k.startswith("persistedOutput")}
    new.update(stdout=text, stderr="")
    return new


def dedup_read(cfg, epoch, tool_input, response, call_id=None):
    if not cfg.get("read_dedup") or response.get("type") != "text":
        return None, None
    f = response.get("file") or {}
    content = f.get("content") or ""
    if len(content) < cfg["dedup_min_chars"]:
        return None, None
    key = f"{f.get('filePath')}|{tool_input.get('offset')}|{tool_input.get('limit')}"
    h = digest(content)
    prev = epoch["reads"].get(key)
    if prev and call_id and prev.get("id") == call_id:
        prev = None
    epoch["reads"][key] = {"hash": h, "at": time.strftime("%H:%M:%S"), "id": call_id}
    if not prev or prev["hash"] != h:
        return None, None
    note = (
        f"[ctxlc] File unchanged since you read this same range at {prev['at']} in the current context; "
        f"content omitted to avoid duplicating {len(content):,} chars. It is still above in this conversation."
    )
    new = {"type": "text", "file": dict(f, content=note + "\n", numLines=1)}
    return new, {"kind": "read_dedup", "raw_chars": len(content), "new_chars": len(note)}


# Claude Code 2.1.278 replaces an MCP result above its token limit with this notice (checked live); the full text
# is in the named file, which the model would otherwise have to grep or read in further turns.
MCP_SAVED = re.compile(r"^Error: result \([\d,]+ characters across [\d,]+ lines\) exceeds maximum allowed tokens\. "
                       r"Output has been saved to (\S+?)\.?\n")


def mcp_text(response):
    """Text of an MCP tool result (a string, a list of content blocks, or {"content": [...]}), or the saved file's
    text when the result was too large to inline; None if it has anything but text, such as an image."""
    if isinstance(response, str):
        m = MCP_SAVED.match(response)
        if m:
            try:
                with open(m.group(1), errors="replace", encoding="utf-8") as f:
                    return f.read(PERSISTED_READ_MAX)
            except OSError:
                return None
        return response
    blocks = response.get("content") if isinstance(response, dict) else response
    if not isinstance(blocks, list) or not all(isinstance(b, dict) and b.get("type") == "text" for b in blocks):
        return None
    return "\n".join(b.get("text") or "" for b in blocks)


def mcp_replace(response, text):
    """`response` with its text replaced, in the same shape."""
    if isinstance(response, str):
        return text
    blocks = [{"type": "text", "text": text}]
    return dict(response, content=blocks) if isinstance(response, dict) else blocks

