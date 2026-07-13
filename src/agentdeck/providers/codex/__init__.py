"""Codex CLI session provider."""

WEB_SEARCH_CONFIG_OVERRIDE = 'web_search="live"'
# Let AgentDeck-owned Codex chats reach the network in the workspace-write
# sandbox by default. Codex blocks outbound network in workspace-write unless
# this is set, which makes curl/pip/git push/gh either hang until timeout or
# stall on a per-command approval — so delegated review/fix/PR and web tasks
# could not push or open PRs without manual intervention. Enabling it here makes
# network the default for every AgentDeck-launched Codex process.
NETWORK_ACCESS_CONFIG_OVERRIDE = "sandbox_workspace_write.network_access=true"
