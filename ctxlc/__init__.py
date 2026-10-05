"""ctxlc — context lifecycle manager for Claude Code.

Keeps a small, durable, incrementally-maintained representation of a project
session so that long-running sessions, idle resumes and model switches do not
have to replay (and re-cache) the whole raw transcript.

Layers:
  L0  live conversation            (owned by Claude Code; shrunk only by compaction or /clear)
  L1  tool-output compaction       (PostToolUse rewrites bulky/duplicate output before the model sees it)
  L2  durable project state        (.claude/context/state.json, rendered into a token-budgeted digest)
  L3  recoverable history          (.claude/context/history.jsonl + artifacts/, searched on demand)
"""

__version__ = "0.1.4"
