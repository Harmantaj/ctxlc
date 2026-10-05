"""Build ctxlc as a Claude Code plugin, for Cowork.

Cowork runs Claude Code on the host with its own config directory, so `ctx install` (which edits
~/.claude/settings.json) does not reach it; uploaded plugins do. The zip carries the package and
registers the same hooks as `ctx install`, run from the plugin's own copy. claude.ai-hosted plugins may not ship a
top-level bin/, so the hooks run the package with `python3 -m ctxlc` instead of bin/ctx.
"""

import json
import os
import zipfile

from . import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# CTXLC_PLUGIN marks this copy: where `ctx install` hooks run too (a local session also loads claude.ai plugins),
# the plugin's copy stands down so state is saved and injected once.
# On Windows (hooks run in Git Bash, $OS is Windows_NT) python3 is often missing or the Microsoft Store stub; python.org
# installs provide the py launcher.
HOOK_CMD = ('CTXLC_PLUGIN=1 PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" '
            '$([ "$OS" = Windows_NT ] && { command -v py >/dev/null && echo "py -3" || echo python; } || echo python3) '
            '-m ctxlc hook')


def manifest():
    return {
        "name": "ctxlc",
        "version": __version__,
        "description": "Keeps requirements, decisions, the current task and open failures across compaction and "
                       "/clear, and steers compaction summaries toward the work in progress.",
    }


def hooks_json():
    from .cli import HOOK_EVENTS
    out = {}
    for ev, matcher in HOOK_EVENTS.items():
        g = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 30}]}
        if matcher:
            g["matcher"] = matcher
        out[ev] = [g]
    return {"hooks": out}


def build(out_path, sync_folder=None):
    pkg = os.path.join(ROOT, "ctxlc")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(".claude-plugin/plugin.json", json.dumps(manifest(), indent=2) + "\n")
        z.writestr("hooks/hooks.json", json.dumps(hooks_json(), indent=2) + "\n")
        if sync_folder:  # None keeps the default ("auto"); "off" disables sync
            value = None if sync_folder == "off" else sync_folder
            z.writestr("ctxlc/plugin_config.json", json.dumps({"cowork_sync_folder": value}, indent=2) + "\n")
        for name in sorted(os.listdir(pkg)):
            if name.endswith((".py", ".md")):
                z.write(os.path.join(pkg, name), "ctxlc/" + name)
    return os.path.abspath(out_path)
