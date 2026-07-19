# Staging / canary environment

A second, isolated agentdeck stack that runs **alongside** the live one on this
host, so new features and DB/schema changes can be exercised before they reach
the live services. Same code repo, same account dirs (monitored read-only) — but
its own port, DB, usage cache, and Codex control socket.

## Layout

| Aspect        | Live                                   | Staging                                        |
|---------------|----------------------------------------|------------------------------------------------|
| Code          | `~/agentdeck` (`master`)               | `~/agentdeck-staging` worktree (`staging` branch) |
| venv          | `~/agentdeck/.venv`                    | `~/agentdeck-staging/.venv` (own `uv sync`)    |
| Config        | `~/.config/agentdeck/config.toml`      | `~/.config/agentdeck/config-staging.toml`      |
| Web port      | `127.0.0.1:8756`                       | `127.0.0.1:8757`                               |
| History DB    | `…/agentdeck.db`                       | `…/agentdeck-staging.db`                        |
| Usage cache   | `$XDG_RUNTIME_DIR/agentdeck`           | `~/.cache/agentdeck-staging`                    |
| Codex socket  | `$XDG_RUNTIME_DIR/agentdeck/…sock`     | `$XDG_RUNTIME_DIR/agentdeck-staging/…sock`      |
| systemd       | `agentdeck{,-codex}.service`           | `agentdeck-staging{,-codex}.service`           |
| Phone / TLS   | `https://<tailnet-host>/` (:443)       | `https://<tailnet-host>:8758/`                 |
| Inject        | on                                     | **on** (required by the New Claude chat feature) |

The Codex socket is isolated purely via the `AGENTDECK_CODEX_SOCKET` env var
(honored by both the runtime binder and the web client) plus a per-service
`RuntimeDirectory=agentdeck-staging` — no app code change needed.

> Port 8758 (not 8443) fronts staging because the outdoor nginx dev-TLS
> container already binds `*:8443` (`NGINX_HTTPS_PORT`).

## Setup / repair

```bash
~/agentdeck-staging/scripts/staging-setup.sh   # idempotent
```
Creates the worktree, `uv sync`s, seeds the config, installs + enables the units,
and sets up `tailscale serve`.

## Day-to-day

- Develop features on the `staging` branch inside `~/agentdeck-staging`, then
  `systemctl --user restart agentdeck-staging` (or `…-codex` for runtime changes).
- Reach it from the phone at `https://<tailnet-host>:8758/` (installable as its
  own PWA, separate from the live one).
- **DB / migration testing:** staging has its own `agentdeck-staging.db`; run the
  changed code against it and confirm before promoting. To start from a fresh
  schema, stop `agentdeck-staging`, delete `agentdeck-staging.db*`, restart.
  To test against live-shaped data, copy the live DB over it while both are
  stopped.

## Promote (canary → live)

```bash
~/agentdeck-staging/scripts/staging-promote.sh          # prompts, fast-forward
~/agentdeck-staging/scripts/staging-promote.sh --yes --push
```
Refuses on a dirty tree, merges `staging` → `master` in `~/agentdeck`, `uv sync`s,
restarts the live services, and health-checks. The live DB is migrated by the app
on start, exactly as staging validated.

## Safety notes

- Staging shares the live `~/.claude` / `~/.claude2` / `~/.codex` account dirs.
  **Monitoring** is read-only, but staging is *not* a read-only sandbox. The
  "New Claude chat" runtime feature is **inject-gated**, so it needs
  `inject.enabled = true` — which also allows manual inject into pre-existing
  sessions (no automatic action; `assistant.auto_answer` stays false). With
  `[claude_workers]` enabled, a staging-spawned Claude worker is a real `claude`
  process running under the live account (`permission_mode = bypassPermissions`
  = allow-all), so it **edits files in its cwd and consumes the live account's
  token budget**. `usage_ceiling_pct = 90` refuses new/revived workers once the
  account is at/above 90 % usage so a canary can't drain the budget; delivering
  to an already-live worker stays exempt so in-flight work finishes. Spawn
  workers against throwaway cwds, not real repos, unless you mean it.
- Staging owns only the Codex/Claude workers **it** spawns (tracked in its own DB
  + `state_dir`); live delegation (`/use-codex`, kanban) targets the live
  port/socket and is unaffected.
