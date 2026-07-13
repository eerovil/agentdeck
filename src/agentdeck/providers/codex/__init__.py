"""Codex CLI session provider."""

import os

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
WRITABLE_ROOTS_CONFIG_OVERRIDE = (
    f'sandbox_workspace_write.writable_roots=["{_RUNTIME_DIR}"]'
)
