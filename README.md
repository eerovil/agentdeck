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
- **Message injection** into idle sessions (opt-in) with spawn-time safety
  interlocks — see below.

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
ln -s ~/agentdeck/systemd/agentdeck.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentdeck
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
data renders where the provider exposes it (Codex currently does not); sessions
are grouped by account. Labels must be unique and slug-safe (they appear in
URLs and cache filenames).

## Message injection & safety interlocks

From v0.3 you can send a message to an **idle** session from its detail page.
agentdeck runs `claude -p --resume <id> "<message>"` in the session's working
directory (appending a turn to the same transcript; the live tail then shows
the reply). Interactive streaming chat is a later iteration.

Interlocks, re-checked at spawn time on every attempt:

- **Never writes a live session.** If any process is currently writing that
  session's transcript, injection is refused (two writers corrupt the JSONL) —
  open it in claude.ai instead.
- **cwd must exist** — a session whose worktree was deleted is refused.
- **cwd must be trusted** — agentdeck reads `hasTrustDialogAccepted` from
  `.claude.json` and refuses otherwise. It never *writes* that file; you trust
  a directory by running `claude` in it once. 
- **Kill-switch**: `[inject] enabled = false` hides the form and 403s the route.
  The bundled deploy ships with injection **off** — opt in when ready.

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
