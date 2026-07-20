# Runtime Architecture

This document defines AgentDeck's process boundaries, ownership model, state flow, and restart consequences.

## Two-process deployment

| Boundary | Entry point | Owns | Restart effect |
| --- | --- | --- | --- |
| Web (`agentdeck.service`) | `src/agentdeck/app.py::create_app` | FastAPI routes/templates, SQLite handle, `AppState`, `Collector`, `InjectionService`, push, Deckhand, titles, and Unix-socket clients | Rebuilds dashboard state and web tasks; must leave active agent work running |
| Persistent runtime (`agentdeck-codex.service`) | `src/agentdeck/__main__.py::_codex_runtime` -> `src/agentdeck/runtime.py::create_runtime_app` | `CodexRuntime`, `ClaudeWorkerRuntime`, agent subprocesses, active turns, and pending interactions | Stops runtime-owned Codex app servers and Claude worker process groups |

`systemd/agentdeck.service` wants and starts after the runtime unit, but the units have separate control groups. Both use `KillMode=control-group`; a web restart therefore does not signal the runtime, while a runtime restart tears down its descendants.

The `agentdeck-codex` service and socket names are historical. Do not infer that this process owns only Codex: it also owns all deck-managed Claude workers.

## Web-process composition and state flow

`AppConfig.build_accounts()` creates stable account keys as `<provider_id>:<label>`. `src/agentdeck/providers/__init__.py::PROVIDERS` maps those provider IDs to implementations; collectors and routes should use that registry instead of adding provider conditionals.

`Collector` runs the provider lifecycle for each account, then maintains `AppState` through three independent paths:

- full session scans are authoritative;
- liveness sweeps cheaply update process status between scans;
- provider usage pollers and the host sampler update resource state.

Failures are isolated per loop/account so one malformed transcript or unavailable control client does not stop collection for the rest of the dashboard. Provider/session details live in [providers-sessions.md](providers-sessions.md).

`src/agentdeck/state.py::AppState` is the web process's in-memory read model for sessions, usage, host stats, transcript cursors, delegation relationships, and generated titles. The database persists selected history and metadata, but it is not a substitute for rebuilding the live read model after web startup.

State producers mutate `AppState` first, then publish coarse `sessions`, `usage`, or `assistant` invalidations through `src/agentdeck/events.py::EventBus`. Subscriber queues are bounded and drop their oldest item when full. Treat events as "render current state" signals, not durable deltas whose count or ordering must be preserved; the SSE/UI path is designed around complete re-renders. See [web-ui-live-updates.md](web-ui-live-updates.md) before changing that contract.

## Runtime control plane

`_codex_runtime` removes any stale socket, applies umask `077`, loads configuration, and serves the runtime FastAPI app on `runtime_socket_path()`. The default is `$XDG_RUNTIME_DIR/agentdeck/codex-runtime.sock` (falling back to `~/.cache`); `AGENTDECK_CODEX_SOCKET` overrides it. The systemd unit creates the runtime directory with mode `0700`.

The web process and provider facades communicate with the runtime via `httpx` over that Unix socket:

- `/accounts/{label}/...` controls Codex app-server threads;
- `/claude/accounts/{label}/...` controls deck-owned Claude workers;
- `src/agentdeck/web/routes_api.py` exposes thin `/api/claude/...` HTTP proxies for local external dispatchers.

`CodexRuntime.start()` eagerly starts one `CodexAppServer` per configured Codex account. `ClaudeWorkerRuntime.host()` instead creates and caches one `ClaudeWorkerHost` lazily per configured Claude account, only when workers are enabled and that account is requested.

Runtime `/healthz` reports successful runtime/Codex startup and the started Codex accounts. It does not instantiate or probe every lazy Claude worker host, so it is not proof that a particular Claude worker account is usable.

Claude worker lineage is durable but live control state is not: `ClaudeWorkerHost` stores `WorkerRecord` entries in `<claude_workers.state_dir>/<label>.json` (default `~/.local/share/agentdeck/claude-workers`) while live subprocess/task state remains in `_live`. On host creation, `_reconcile_orphans()` kills still-running owned process groups left by a crashed runtime rather than adopting dead stdio, while preserving records so the next delivery can revive the session. Deeper delivery, capacity, and interaction semantics belong in [owned-agent-control.md](owned-agent-control.md) and `docs/WORKER_RUNTIME.md`.

## Restart invariants and gotchas

- Web-only, template, static, collector, and other web-owned changes restart only `agentdeck.service`; active runtime turns and pending interactions must survive.
- Changes to `runtime.py`, Codex app-server ownership, or Claude worker hosting require the runtime restart path. Inspect active work first.
- Never run a raw `systemctl restart agentdeck-codex.service` from a runtime-owned agent turn: the unit's control-group teardown can kill the caller mid-deploy. `agentdeck restart-runtime` writes a continuation marker and triggers a detached restart; runtime startup schedules `ClaudeWorkerRuntime.resume_after_restart()` without delaying health.
- Runtime shutdown cancels the restart-resume task, then stops Codex clients and Claude worker hosts. Do not move long-lived agent ownership back into the web lifespan.
- The HTTP dashboard currently has no authentication: `src/agentdeck/web/deps.py::require_access` is a no-op. Keep it bound to localhost or a trusted private overlay; exposing the web port also exposes agent-control routes.

For deployment ordering and live checks, read [deployment-testing.md](deployment-testing.md) rather than duplicating the operational checklist here.
