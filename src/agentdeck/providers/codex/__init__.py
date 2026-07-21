"""Codex CLI session provider."""

import os
from pathlib import Path

WEB_SEARCH_CONFIG_OVERRIDE = 'web_search="live"'
# Let AgentDeck-owned Codex chats reach the network in the workspace-write
# sandbox by default. Codex blocks outbound network in workspace-write unless
# this is set, which makes curl/pip/git push/gh either hang until timeout or
# stall on a per-command approval — so delegated review/fix/PR and web tasks
# could not push or open PRs without manual intervention. Enabling it here makes
# network the default for every AgentDeck-launched Codex process.
NETWORK_ACCESS_CONFIG_OVERRIDE = "sandbox_workspace_write.network_access=true"

# Grant the rootless-podman runtime dir ($XDG_RUNTIME_DIR, e.g. /run/user/1000) as a
# workspace-write writable root. AgentDeck-owned chats run tests inside the service
# containers via `podman exec`, but the sandbox mounts the runtime dir read-only, so
# podman fails with `chmod /run/user/<uid>/libpod: read-only file system` and Codex has
# to escalate out of the sandbox. Making it writable lets `podman exec` work in-sandbox.
_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"

# A git worktree keeps its refs, index, HEAD and logs in the MAIN repo's `.git`
# (`<repo>/.git/worktrees/<name>` plus the shared `<repo>/.git/refs`), which lives
# OUTSIDE the worktree cwd. Codex's workspace-write sandbox makes only the cwd writable,
# so any git write from a worktree session — fetch/merge/commit, ref updates, or the
# `index.lock` / `ORIG_HEAD.lock` / `FETCH_HEAD` files — fails with `Read-only file
# system`. AgentDeck-owned chats routinely run in kanban `.worktrees/<issue>` checkouts,
# so grant the user's home tree — where every repo (and its `.git`) lives — as a writable
# root. It is canonicalized because `/home` is a symlink to `/var/home` here and the
# sandbox matches on the real path git actually writes to. This is a deliberately broad,
# process-global grant (the app-server is cwd-agnostic and serves many worktrees at once);
# set AGENTDECK_WRITABLE_BASE to narrow or widen the base.
_WORKSPACE_BASE = os.environ.get("AGENTDECK_WRITABLE_BASE") or os.path.realpath(Path.home())

_WRITABLE_ROOTS = [root for root in (_RUNTIME_DIR, _WORKSPACE_BASE) if root]
WRITABLE_ROOTS_CONFIG_OVERRIDE = (
    "sandbox_workspace_write.writable_roots=["
    + ",".join(f'"{root}"' for root in _WRITABLE_ROOTS)
    + "]"
)
