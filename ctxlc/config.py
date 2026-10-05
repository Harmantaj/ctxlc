"""Tunable thresholds. Defaults come from the trace replay in bench/simulate.py.

Every value can be overridden per project in .claude/context/config.json.
"""

import json
import os
import shutil
import sys


_CHECKOUT_CTX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "ctx")
# Running from a source checkout (bin/ctx next to the package) rather than from a pip/pipx install.
FROM_CHECKOUT = os.path.isfile(_CHECKOUT_CTX)
WINDOWS = os.name == "nt"


def _ctx_path():
    """Absolute path of the `ctx` executable that hooks and the /ctx skill call.

    A checkout uses its own bin/ctx. An install uses the console script next to this interpreter, falling back
    to PATH: hooks run under the app's environment, which may not have ~/.local/bin on PATH, so a bare `ctx`
    is not enough."""
    if FROM_CHECKOUT:
        return _CHECKOUT_CTX
    beside = os.path.join(os.path.dirname(sys.executable), "ctx.exe" if WINDOWS else "ctx")
    return beside if os.path.isfile(beside) else (shutil.which("ctx") or beside)


def _shell_path(path):
    # On Windows, Claude Code runs hook and Bash tool commands in Git Bash, where forward slashes need no escaping.
    return path.replace("\\", "/") if WINDOWS else path


CTX_PATH = _shell_path(_ctx_path())
# Interpreter for a checkout's bin/ctx: python3 on macOS/Linux; Windows often has no python3 (python.org installs
# provide python and py), so use the one running now.
PYTHON = _shell_path(sys.executable) if WINDOWS else "python3"
PYTHON_CMD = f'"{PYTHON}"' if WINDOWS else PYTHON

DEFAULTS = {
    # --- L1 tool output -------------------------------------------------
    # Bash output larger than this (chars) is archived and replaced by an extract.
    "bash_compact_min_chars": 6000,
    # Target size of the extract that replaces it.
    "bash_extract_max_chars": 2500,
    # File viewers / searches / git diff|show (toolout.is_viewer) pass through unchanged up to this size;
    # above it Claude Code itself truncates, so the extract is the better view.
    "viewer_passthrough_max_chars": 30000,
    # Identical output repeated within one context epoch is replaced by a pointer.
    "dedup_min_chars": 400,
    # Re-reads of an unchanged file range within one epoch are replaced by a pointer.
    "read_dedup": True,
    # Let failing build/test/install output reach the extractor (see toolout.NOISY).
    "wrap_noisy_commands": True,
    # --- L2 state ---------------------------------------------------------
    # Token budget for the digest injected at SessionStart (startup/clear/compact).
    "digest_budget_tokens": 2500,
    # Hard cap on the whole SessionStart injection (digest + protocol). Claude Code 2.1.278 shows the model only a
    # 2 KB preview of hook context over 10,000 chars (checked live), so stay below that.
    "injection_max_chars": 9500,
    "recent_prompts_kept": 6,
    # A handoff is injected into the next session only (and its compactions), plus any
    # session that starts within this many seconds after that one consumed it.
    "handoff_ttl_s": 6 * 3600,
    # --- retention (run at session startup) ---------------------------------
    # Archived full outputs older than this are deleted (state items are kept forever).
    "artifact_max_age_days": 14,
    # history.jsonl above this size is cut to its newest half.
    "history_max_bytes": 20_000_000,
    # --- triggers -----------------------------------------------------------
    # Warn before a model switch forfeits a warm cache of at least this many tokens.
    "switch_guard_min_tokens": 60000,
    # Warn on the first prompt after the cache expired if context is at least this big.
    "idle_guard_min_tokens": 60000,
    # A second submit within this many seconds of the warning goes through.
    "guard_override_window_s": 900,
    # "auto" = like "ask", but once the user sent anyway on 2 of the last 3 warnings, say it without holding the
    # message; "ask" = always hold the first submit and let the next one through; "off" = never warn.
    "idle_guard": "auto",
    # Non-blocking notice, once per session per size, when a warm conversation grows past these context sizes.
    # [] turns it off.
    "context_nudge_tokens": [150000, 250000, 400000],
    "switch_guard": "ask",
    # --- Cowork cloud tasks -------------------------------------------------
    # Where state is kept between tasks, on the user's computer: "auto" = in whichever folder the task works in
    # (one state file per project folder), a path = always that folder, None = off.
    # Set when building the plugin: `ctx plugin [--sync-folder PATH | --no-sync]`. See sync.py.
    "cowork_sync_folder": "auto",
}
# Written into the plugin zip by `ctx plugin`: a Cowork task's .claude/context starts empty, so a per-project
# config.json there cannot carry settings.
PLUGIN_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_config.json")


def load(store_dir):
    cfg = dict(DEFAULTS)
    for path in (PLUGIN_CONFIG, os.path.join(store_dir, "config.json")):
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    return cfg
