# agentdeck

Self-hosted, mobile-first dashboard for monitoring and steering coding-agent
CLI sessions. v1 targets [Claude Code](https://docs.anthropic.com/en/docs/claude-code);
the architecture is a provider abstraction over "session sources" so other
agent CLIs can be added later.

*Screenshot: TODO*

## Status & scope

**v0.1 — read-only dashboard.** What works today:

- Usage limit bars (5-hour / 7-day) per account, updating live over SSE.
- Live/idle session list across any number of Claude Code config dirs
  ("accounts"), with titles, last prompts, and claude.ai deep-links where
  derivable.
- Zero write paths: agentdeck never touches your sessions in v0.1.

Roadmap: v0.2 transcript viewer, v0.3 message injection / interactive chat,
v0.4 provider abstraction docs. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

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
  `CLAUDE_CONFIG_DIR` (e.g. `~/.claude` and `~/.claude2`).

The config file contains **no secrets** — credentials always come from the
provider's own store.

## How it works

agentdeck reads undocumented, unversioned internals of the Claude Code CLI
(session registry files, `history.jsonl`, the OAuth usage endpoint). These
can change without notice in any CLI release; every parser is written to
skip-and-count unknown shapes rather than crash. Details will be documented
in `docs/claude-code-internals.md` (v0.4).

## Multi-account

Each `[[accounts]]` entry is an independent `CLAUDE_CONFIG_DIR`. Limit bars
render per account; sessions are grouped by account. Labels must be unique
and slug-safe (they appear in URLs and cache filenames).

## Message injection & safety interlocks

Not implemented yet (v0.3). The design (see the build plan) refuses to write
to any session whose owning pid is alive, requires the target cwd to exist
and be trusted, and has an `[inject] enabled = false` kill-switch.

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
