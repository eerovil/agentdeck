# Deployment and Testing

This document defines AgentDeck's validation tiers, canary-to-live flow, service restart boundary, and required deployment proof.

Read [architecture-runtime.md](architecture-runtime.md) for process ownership, [owned-agent-control.md](owned-agent-control.md) for worker continuity, and [providers-sessions.md](providers-sessions.md) for provider-facing behavior. Use `README.md` and `docs/staging.md` for exhaustive installation and setup commands.

## Sources of truth

- `AGENTS.md` is the acceptance policy: use its **Fast validation tiers** and **Shipping and deployment** sections when this document and an older transcript disagree.
- `scripts/staging-setup.sh` defines the canary bootstrap; `scripts/staging-promote.sh` defines promotion ordering and refusal checks.
- `systemd/agentdeck{,-codex}.service` and `systemd/agentdeck-staging{,-codex}.service` define the actual process split.
- `.github/workflows/ci.yml` is the CI contract; focused regressions live under `tests/` as configured by `pyproject.toml:[tool.pytest.ini_options]`.

## Live and canary topology

| Boundary | Live | Staging / canary |
| --- | --- | --- |
| Checkout | `~/agentdeck`, branch `master` | `~/agentdeck-staging`, branch `staging` |
| Web | `agentdeck.service`, `127.0.0.1:8756` | `agentdeck-staging.service`, `127.0.0.1:8757` |
| Runtime | `agentdeck-codex.service` | `agentdeck-staging-codex.service` |
| State | live config, DB, usage cache, runtime socket | separate config, DB, cache, worker state, and socket |

The staging runtime socket is isolated by `AGENTDECK_CODEX_SOCKET` plus `RuntimeDirectory=agentdeck-staging`. Its monitored Claude/Codex account directories are shared with live, however: monitoring is read-only, but injection and staging-spawned Claude workers can edit their real cwd and consume the live account budget. Follow `docs/staging.md: Safety notes` and use throwaway working directories unless real edits are intended.

## Exact validation tiers

### Documentation only

- Review the rendered Markdown or relevant text.
- Run `git diff --check`.
- Do not run application tests or restart services.

### Presentation-only template, CSS, or JavaScript

- Run the focused web test or browser scenario covering the changed behavior and real rendered/HTMX/SSE lifecycle.
- Run Ruff only when Python changed, then run `git diff --check`.
- Run the full suite when shared rendering/contracts changed, focused evidence is ambiguous, or a related UI batch is ready to ship. Do not repeatedly run it for an isolated style correction.

### Python behavior, provider, queue, parser, or runtime

- Run the closest focused tests first.
- Run the full suite once with `.venv/bin/pytest -q`.
- Run Ruff on changed Python files and run `git diff --check`.
- If `.venv/bin` is unavailable, use the corresponding `uv run` command. Do not rerun a green test without a concrete reason; distinguish product failures from fixture/harness failures.

CI independently runs `uv sync --all-extras --dev`, `uv run ruff check .`, and `uv run pytest -q` (`.github/workflows/ci.yml`). Useful focused entry points include `tests/test_web.py` for rendered/live-update behavior, provider-specific test modules, `tests/test_codex_runtime.py` for the service boundary, and `tests/test_restart.py` plus `tests/test_claude_worker.py` for restart continuity.

## Canary validation

- Bootstrap or repair with `scripts/staging-setup.sh`; keep command-by-command setup details in `docs/staging.md`.
- Restart only `agentdeck-staging.service` for staging web-owned changes; restart `agentdeck-staging-codex.service` only for persistent-runtime changes, after checking active work.
- Exercise DB/schema changes against staging's own database before promotion. The fresh-schema and live-shaped-copy procedures are in `docs/staging.md: DB / migration testing` and require the documented service stops.
- Validate the affected page or workflow on port `8757`; a healthy canary alone is insufficient when the behavior is interactive or worker-backed.

## Promotion ordering

`scripts/staging-promote.sh` is authoritative and deliberately conservative:

1. Refuse dirty staging, dirty live, or a live checkout not on `master`.
2. Show `master..staging`, then merge `staging` into live `master` with `--ff-only` by default (`--no-ff` is explicit).
3. Run `uv sync` in the live checkout.
4. Restart only `agentdeck.service`, wait briefly, and check `http://127.0.0.1:8756/healthz`.
5. Push `master` only with `--push`; the script can also finish a push left by an interrupted prior promotion.
6. Restart the persistent runtime only with `--restart-runtime`, after the web deploy and any push.

The script's successful health check is one promotion step, not complete deployment proof. The live verification contract below still applies.

## Deployment split

- Frontend, template, stylesheet, transcript-rendering, and other web-process changes restart only `agentdeck.service`; do not restart `agentdeck-codex.service`.
- Restart `agentdeck-codex.service` only when persistent-runtime-owned code changed and deployment requires it. Check active chats and plan continuity first.
- Both runtime units use `KillMode=control-group`; a raw runtime restart from a runtime-owned Claude turn can kill the caller and its shell.
- From an owned Claude turn, use `agentdeck restart-runtime`. `src/agentdeck/__main__.py::_restart_runtime` writes a durable marker and `src/agentdeck/providers/claude_code/restart.py::trigger_detached_restart` launches the restart in a transient user unit. `src/agentdeck/runtime.py::ClaudeWorkerRuntime.resume_after_restart` consumes the marker and re-delivers one stable continuation; `tests/test_restart.py` covers marker, safety, cleanup, and resume paths.
- Documentation-only guide changes are committed and pushed but require no service restart.

## Live verification contract

Before a frontend restart, record both live service PIDs. After deployment, verify all of the following:

- `agentdeck.service` has the expected new PID.
- `agentdeck-codex.service` retained its PID unless a runtime restart was intentional.
- `http://127.0.0.1:8756/healthz` is healthy.
- The affected live page or workflow exhibits the new behavior; use the exact session/page when the change is session-specific.
- Local `master`, `origin/master`, and the worktree are in the expected clean/ref state.
- Service status and logs explain any uncertain restart or behavior; a returned restart command is never deployment proof.

A completed application feature is committed, pushed to `master`, deployed, and verified live. Perform that cycle once after acceptance criteria pass rather than deploying intermediate attempts.
