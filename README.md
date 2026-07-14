# agentdeck

Self-hosted, mobile-first dashboard for monitoring and steering coding-agent
CLI sessions. It reads local sessions from
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex CLI
through a provider abstraction over session sources.

*Screenshot: TODO*

## Status & scope

What works today:

- **Usage limit bars** (5-hour / 7-day) per account, live over SSE, with a
  sparkline of recent 5h usage and stale-greying on backoff.
- **Live/idle session list** across any number of Claude Code and Codex config
  dirs ("accounts"), with titles, last prompts, and Claude deep-links where
  available. Codex liveness is inferred from recent rollout writes because the
  CLI has no process-to-session registry.
- **Transcript viewer** (`/sessions/{key}`): per-event role/tool/model
  rendering, token totals, todos, live tail for running sessions, and
  "load earlier" pagination.
- **Codex chat controls** (opt-in): start persisted chats, queue follow-up
  messages on completed non-interactive sessions, and watch replies arrive in
  the existing transcript view — see the safety limits below.

Roadmap: interactive streaming chat, more agent CLI providers (Gemini), and
`docs/claude-code-internals.md`. See
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

## Security model

agentdeck has **no authentication**. It is designed to be bound to a
[Tailscale](https://tailscale.com/) (or other private overlay) address and
nothing else:

- Default bind is `127.0.0.1` — an unconfigured install exposes nothing.
- Set `server.bind` to your Tailscale IP to reach it from your phone.
- **Do not bind `0.0.0.0` on an untrusted LAN.** From v0.3 the inject routes
  are effectively remote code execution for anyone who can reach the port.
- All routers pass through a single `require_access` dependency (a no-op
  today) so real auth can be added later without touching routes.
- Credentials (`.credentials.json` OAuth tokens) are read fresh per poll,
  held only in local variables, and never logged or served.

Threat model in one line: anyone who can reach the port is you.

## Install

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/eerovil/agentdeck.git ~/agentdeck
cd ~/agentdeck
uv sync
mkdir -p ~/.config/agentdeck
cp config.example.toml ~/.config/agentdeck/config.toml
$EDITOR ~/.config/agentdeck/config.toml   # set bind address + accounts
uv run agentdeck
```

Run as a systemd user service (survives logout with lingering enabled):

```sh
mkdir -p ~/.config/systemd/user
ln -s ~/agentdeck/systemd/agentdeck.service ~/agentdeck/systemd/agentdeck-codex.service \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentdeck-codex agentdeck
loginctl enable-linger "$USER"   # keep it running when logged out
```

## Configuration

Config lives at `~/.config/agentdeck/config.toml` (override with the
`AGENTDECK_CONFIG` env var). See [config.example.toml](config.example.toml)
for the annotated reference. Highlights:

- `[server]` — bind address and port (default `127.0.0.1:8756`).
- `[polling]` — usage poll interval (default 300 s, server-side rate limited;
  agentdeck jitters and backs off on 429), session scan and liveness sweep
  intervals.
- `[usage] shared_cache_dir` — agentdeck publishes each usage snapshot
  atomically to `$XDG_RUNTIME_DIR/agentdeck/usage-<label>.json` (fallback
  `~/.cache/agentdeck`) so other tools can read limits without their own
  polling.
- `[[accounts]]` — one block per agent config dir; for Claude Code, one per
  `CLAUDE_CONFIG_DIR` (e.g. `~/.claude` and `~/.claude2`), and for Codex the
  normal `CODEX_HOME` (usually `~/.codex`).

The config file contains **no secrets** — credentials always come from the
provider's own store.

## How it works

agentdeck reads undocumented, unversioned internals of the Claude Code CLI
(session registry files, `history.jsonl`, the OAuth usage endpoint) and Codex
CLI (date-partitioned rollout JSONL). These can change without notice in any
CLI release; every parser is written to skip-and-count unknown shapes rather
than crash. Details of the Claude surfaces will be documented in
`docs/claude-code-internals.md` (v0.4).

## Multi-account

Each `[[accounts]]` entry is an independent provider config directory. Usage
data renders for both providers; Codex delegates authentication and rolling
limit reads to `codex app-server`. Sessions are grouped by account. Labels must
be unique and slug-safe (they appear in URLs and cache filenames).

## Message injection & safety interlocks

From v0.3 you can start a persisted Codex chat from the dashboard or send
follow-ups to a completed, non-interactive Codex `exec` session from its detail
page. AgentDeck-created chats run through one persistent Codex app-server per
account, enabling active-turn steering, Stop, structured questions, approvals,
and a FIFO follow-up queue. Enter submits and Shift+Enter inserts a newline.
Existing external `exec` rollouts retain the conservative completed-turn
fallback described below.

The bundled systemd deployment keeps Codex controls in a separate
`agentdeck-codex.service`. The dashboard talks to it over a mode-0700 Unix
socket under `$XDG_RUNTIME_DIR/agentdeck`; restarting `agentdeck.service` for a
frontend deploy therefore does not stop active Codex turns or lose pending
approval state. Restart the Codex service itself only after its active turns
have drained.

Local tools can delegate work through the same owned runtime. The prompt is
read from stdin and only the final Codex message is written to stdout:

```sh
uv run --directory ~/agentdeck agentdeck delegate \
  --sandbox workspace-write --cwd /path/to/repo <<'EOF'
Review the current change, fix any issues, and run the tests.
EOF
```

The client polls `POST /api/delegations` / `GET /api/delegations/{id}` and
prints lifecycle and interaction notices to stderr. When Codex needs input, the
question or approval remains answerable in AgentDeck. The delegation bridge
accepts only `read-only` and `workspace-write`; approval-requiring actions use
the interactive AgentDeck review path.

Standalone Codex TUI sessions remain read-only because Codex exposes no
cross-process ownership registry. A safety spike confirmed that a second
resume process can write into a rollout while its original TUI process is still
active. They remain available for monitoring in agentdeck, without an external
open button.

Interlocks, re-checked at spawn time on every attempt:

- **Completed `exec` turns only.** The rollout's last native boundary must be
  `task_complete`; `task_started`, aborted, TUI, and unknown session kinds fail
  closed. This is rechecked immediately before Codex starts.
- **cwd must exist** — a session whose worktree was deleted is refused.
- **One owned turn per session** — agentdeck serializes its own submissions in
  a FIFO queue; only one Codex child writes the rollout at a time. Owned process
  groups are reaped on timeout or shutdown.
- **Messages stay out of argv** — the prompt is delivered on stdin.
- **Kill-switch**: `[inject] enabled = false` hides the form and 403s the route.
  The bundled configuration ships with injection **off** — opt in when ready.

To enable on the live deploy: set `[inject] enabled = true` in
`~/.config/agentdeck/config.toml` and `systemctl --user restart agentdeck`.

## Contributing

Clean-room note: no code, templates, or CSS may be copied from AGPL-licensed
projects (e.g. `claudecodeui`). Test fixtures must be **synthetic only** —
never copy real transcripts, registry files, or credentials into the tree.
A quick hygiene check before committing:

```sh
git grep -I "sk-ant" -- ':!README.md'
```

should return nothing (the string `oauth` appears legitimately in the usage
poller; actual tokens must not).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
