"""State sync for Cowork cloud tasks (config `cowork_sync_folder`).

A cloud Cowork task starts with an empty /home/claude, and the user's attached folder is not mounted in the
sandbox: only the model's remote-device tools reach it, and hooks cannot call tools. So the hooks ask the model
to carry the state across: at task start it copies the folder's sync file into `ctx sync-import`, and when the
durable state changes the Stop hook hands it the export to write back. The export holds only durable parts
(items, brief, handoff, open failures), so ordinary turns do not trigger a sync.

With "auto" (the default) each task picks the folder it works in and registers it with `sync-import --folder`, so
every project folder keeps its own state; a task with no folder runs `sync-import --off` and is not synced.
"""

import hashlib
import json
import os

from .store import _failure_ids, add_item

# Run by bash on the user's computer, where "/" works on every OS (Git Bash included).
SYNC_FILE = ".claude/ctxlc-cowork.json"
EOF_MARK = "CTXLC_EOF"


def active(cfg):
    return (cfg.get("cowork_sync_folder") not in (None, "", "off")
            and os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "remote_cowork")


def is_auto(cfg):
    return cfg.get("cowork_sync_folder") == "auto"


def folder(cfg, st):
    """The folder this task syncs with, or None while an "auto" task has not registered one."""
    return (st.get("sync") or {}).get("folder") if is_auto(cfg) else cfg["cowork_sync_folder"]


def needs_pull(cfg, st):
    sync = st.get("sync") or {}
    return active(cfg) and not sync.get("ready") and not sync.get("off")


def remote_path(base):
    return base.rstrip("/\\") + "/" + SYNC_FILE


def export(st):
    _failure_ids(st)
    return {
        "ctxlc_sync": 1,
        "items": [it for it in st["items"] if it["status"] == "active"],
        "next_id": st["next_id"],
        "next_failure": st.get("next_failure", 1),
        "failures": [f for f in st.get("failures", []) if not f.get("resolved")],
        "brief": st.get("brief"),
        "handoff": st.get("handoff"),
    }


def digest_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def merge(st, data):
    """Make the imported state the base and re-add what this task recorded before the import."""
    local = [it for it in st["items"] if it["status"] == "active"]
    st["items"] = list(data.get("items") or [])
    st["next_id"] = dict(data.get("next_id") or {})
    for it in local:
        add_item(st, it["kind"], it["text"], source=it.get("source", "agent"), ts=it.get("created"))
    _failure_ids(st)
    known = {f.get("cmd") for f in data.get("failures") or []}
    mine = [f for f in st.get("failures", []) if f.get("cmd") not in known]
    st["next_failure"] = max(st.get("next_failure", 1), data.get("next_failure", 1))
    for f in mine:
        f["id"] = f"F{st['next_failure']}"
        st["next_failure"] += 1
    st["failures"] = list(data.get("failures") or []) + mine
    st["brief"] = data.get("brief") or st.get("brief")
    st["handoff"] = st.get("handoff") or data.get("handoff")


def mark_ready(st, base=None):
    """Pushing is allowed only after this task pulled (or found no file): a push before that would overwrite the
    folder's state with this task's nearly empty one."""
    sync = st.setdefault("sync", {})
    sync.update(ready=True, pushed=digest_hash(export(st)))
    if base:
        sync["folder"] = base


def mark_off(st):
    st.setdefault("sync", {}).update(off=True, ready=False)


def pull_instructions(cfg, cli):
    if is_auto(cfg):
        return f"""## Cowork state sync (ctxlc)
ctxlc keeps each project's state in that project's folder on the user's computer, which this sandbox cannot read \
directly. Before answering the first message, restore it:
1. Pick the folder on the user's computer this task works in: the folder connected to this task, or the one the \
user's request names (if the request names a folder that is not connected, request access to it with the \
remote-device folder-access tool). If there is no such folder, run `{cli} sync-import --off` and skip the rest; \
do not request folder access only for ctxlc.
2. On the user's computer (remote-device bash tool): `cat '<folder>/{SYNC_FILE}'`
3. If that printed JSON, pass it unchanged to this sandbox's shell, which merges it and prints the restored state:
   `{cli} sync-import --folder '<folder>' <<'{EOF_MARK}'` + newline + the JSON + newline + `{EOF_MARK}`
   If the file does not exist, run `{cli} sync-import --folder '<folder>' --none` instead.
If the computer is not reachable, say so once and continue without it."""
    path = remote_path(cfg["cowork_sync_folder"])
    return f"""## Cowork state sync (ctxlc)
This project's ctxlc state is kept in the user's folder `{cfg['cowork_sync_folder']}`, which this sandbox cannot \
read directly. Before answering the first message, restore it:
1. On the user's computer (remote-device bash tool): `cat '{path}'`
2. If that printed JSON, pass it unchanged to this sandbox's shell, which merges it and prints the restored state:
   `{cli} sync-import <<'{EOF_MARK}'` + newline + the JSON + newline + `{EOF_MARK}`
   If the file does not exist, run `{cli} sync-import --none` instead.
If the folder is not connected to this task, request access to `{cfg['cowork_sync_folder']}` with the remote-device \
folder-access tool and retry once. If the computer or folder is still not reachable, say so once and continue without it."""


def push_reason(base, data):
    path = remote_path(base)
    return (f"ctxlc: this project's durable state changed. Save it to the user's folder with the remote-device bash "
            f"tool (one call, content unchanged; if the folder is not connected, request access to it first), then finish:\n"
            f"mkdir -p '{os.path.dirname(path)}' && cat > '{path}' <<'{EOF_MARK}'\n"
            f"{json.dumps(data, separators=(',', ':'))}\n{EOF_MARK}")


def stop_push(st, cfg, d):
    """The Stop hook's push request, or None. Never blocks twice in a row (stop_hook_active)."""
    if not active(cfg) or d.get("stop_hook_active"):
        return None
    sync = st.get("sync") or {}
    base = folder(cfg, st)
    if not sync.get("ready") or not base:
        return None
    data = export(st)
    h = digest_hash(data)
    if h == sync.get("pushed"):
        return None
    sync["pushed"] = h
    return push_reason(base, data)
