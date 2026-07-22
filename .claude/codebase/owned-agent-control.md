# Owned Agent Control

This document covers how AgentDeck owns, addresses, and safely controls long-lived Claude workers and Codex threads.

## Control-plane boundary

- `src/agentdeck/runtime.py:create_runtime_app` is the persistent control plane. It creates a `CodexRuntime` plus a config-gated `ClaudeWorkerRuntime`; the restartable web process talks to them over the runtime Unix socket.
- `src/agentdeck/providers/claude_code/worker_client.py:ClaudeWorkerClient` and `src/agentdeck/providers/codex/runtime_client.py:CodexRuntimeClient` are web-side facades. Keep CLI wire parsing, process ownership, pending interactions, and active-turn state in the runtime.
- Transcript visibility is not ownership. Providers may display any discovered session, but active controls are granted only when the runtime maps that session to an owned worker/thread.
- `src/agentdeck/inject.py:InjectionService` is the shared web coordinator: it enforces `[inject].enabled`, validates messages, runs one FIFO per session, bounds status history, and fails later queued messages after the first delivery failure. `web/routes_actions.py` repeats capability/config checks at the HTTP boundary.
- Runtime-owned interaction requests are normalized before crossing the socket. Claude does this in `worker.py:_normalize_interaction`; Codex does it in `providers/codex/appserver.py`. Do not teach the web process either CLI's raw protocol.
- Claude permission interactions expose only accept, decline, and cancel: its control response cannot grant a durable session rule, so offering an `acceptForSession` choice would be false UI.
- See `.claude/codebase/architecture-runtime.md` for service/process topology and `.claude/codebase/providers-sessions.md` for discovery, capabilities, and session-tree presentation.

## Deck-owned Claude workers

- `src/agentdeck/providers/claude_code/worker.py:ClaudeWorkerHost` owns one account's headless `claude -p` stream-JSON processes. `ClaudeWorkerRuntime.host()` lazily creates one host per enabled Claude account.
- Callers choose an opaque key such as `owner/repo#123`; keys stay in JSON request bodies because `#` and `/` are unsafe path components. `docs/WORKER_RUNTIME.md` is the public worker contract; `web/routes_api.py` proxies its poller-facing surface.
- `WorkerRecord` is durable per-key lineage: key, cwd, session ID, last-result metadata, spawn-time permission mode, and delivery receipts. `_save_state()` writes account JSON through a temporary-file replacement. There is intentionally no separate `manual` versus `managed` origin field.
- `ClaudeWorkerHost.deliver()` is the single control primitive:

  | State | Result |
  | --- | --- |
  | live, turn active | steer the current turn |
  | live, idle | queue the next turn on the same process |
  | exited, known session | spawn with `--resume`; fall back to fresh only if failure occurred before writing |
  | unknown or `fresh=true` | spawn a new worker; a new key requires `cwd` |

- Lifecycle operations have distinct durability: `stop_worker` terminates a live process and keeps lineage; `park_worker` is idempotent and keeps lineage even if already stopped; `release_worker` idempotently terminates and forgets; `forget` refuses live keys.
- Spawn/revive admission and account policy are documented under [provider usage admission](providers-sessions.md#usage-admission-gotchas). Steering an already-live worker is exempt so in-flight work can finish, and revives reuse the permission mode recorded at the original spawn.

## Delivery idempotency

- Durable callers must retain one stable `delivery_id` until acceptance. `deliver()` fingerprints the logical request, serializes work per key, and persists a prepared receipt before writing to the child.
- A finalized receipt replays its original result. Reusing an ID with a different fingerprint returns `delivery_id_conflict`; a clean pre-write failure removes the prepared receipt so the same ID can be retried.
- If the write may have started but confirmation was lost, the prepared receipt replays as accepted with action `uncertain`. At-most-once semantics deliberately forbid sending that ID again; further progress needs a new delivery ID after the caller reconciles state.
- Image paths are random upload temporaries, so `_delivery_fingerprint()` includes image count rather than paths or bytes. Do not reuse one delivery ID for different image content merely because the count matches.
- Receipts are pruned by age with a high-count backstop, not a small FIFO, so intervening deliveries do not quickly erase retry protection.

## Process and restart safety

- Workers use `start_new_session=True`. `_terminate()` closes stdin, signals the process group, waits five seconds, escalates to `SIGKILL`, and reaps/cancels the reader.
- On host construction, `_reconcile_orphans()` uses the Claude registry's PID/start-time liveness check and kills stale process groups whose session IDs match persisted records. It retains lineage so the next delivery revives exactly one owner.
- `_read_loop()` removes an exited worker but preserves its record. If the reader fails while the child is still alive, it terminates that detached process before a later `--resume` can create a second writer for the transcript.
- Never raw-restart the persistent runtime from an owned agent turn: systemd cgroup teardown also kills the caller. `agentdeck restart-runtime` writes a durable marker and performs a detached restart; `ClaudeWorkerRuntime.resume_after_restart()` TTL-checks it, resolves the session back to a key, sends one stable `restart-continue:*` delivery, and removes the marker in `finally`.
- Deployment sequencing and when the runtime may be restarted belong in [deployment-testing.md](deployment-testing.md); `scripts/staging-promote.sh` keeps runtime restart opt-in and last.

## AgentDeck-owned Codex threads

- `src/agentdeck/runtime.py:CodexRuntime` starts one `CodexAppServer` per Codex account and owns app-server processes, loaded threads, active turns, pending interactions, and image lifetime across web deploys.
- `src/agentdeck/providers/codex/appserver.py:CodexAppServer` marks newly started threads as owned. On restart, `_recover_owned()` lists both `appServer` and `vscode` source kinds and accepts only transcripts whose first `session_meta` has `payload.originator == "agentdeck"`; transient source labels are not ownership evidence.
- Owned threads support start/queue, compaction, steer, interrupt, wait, and interaction answers. The app-server rejects those operations when the thread is not in `_owned` or lacks the required active turn.
- External completed Codex rollouts can receive conservative ordinary injection when `provider.py:_capabilities` verifies an injectable rollout and an existing cwd. That does not grant active-turn steering, interruption, or interaction control.
- App-server stdout uses `STDOUT_LINE_LIMIT` (16 MiB); dropping this to asyncio's default can make one large JSON-RPC event kill the reader and lose the final response.

## Delegation lineage

- `InjectionService.start_delegation()` creates machine-oriented work and retains bounded pollable status. Once a session key exists, `AppState.mark_delegated_session()` records it in SQLite `delegated_sessions` so rescans and restarts preserve delegated status.
- `parent_session_id` is stored as a raw cross-provider session ID and resolved lazily by `AppState.session_presentation()`. `db.py:record_delegated_session` preserves an existing parent on a bare re-record and replaces it only when an authoritative parent is supplied.
- `agentdeck delegate` defaults `--parent-session` from `CLAUDE_CODE_SESSION_ID` and sends it to `/api/delegations`. Recorded parentage outranks transcript-discovered delegation markers; see `.claude/codebase/providers-sessions.md` for nesting and true-subagent expiry rules.

## External dispatchers and kanban presentation

- External pollers decide what issue work should exist and choose opaque worker keys and delivery IDs; AgentDeck owns process and delivery lifecycle. Poller and worker-skill implementations live outside this repository.
- Worker keys may contain `#` and `/`, so lifecycle requests carry `{key}` in JSON bodies. Durable dispatchers retain one stable `delivery_id` until acceptance; matching retries replay, conflicting payloads fail, and uncertain receipts prevent duplicate delivery.
- `providers/claude_code/kanban.py` recognizes only canonical `kanban-worker` and `kanban-worker-storm` first prompts, extracts mode-independent `owner/repo#number` identity plus supported mode tags, and enriches cards through a bounded best-effort `gh api` cache.
- `worker_type()` gives kanban presentation precedence over cloud/deep-link presentation. That classification does not establish runtime ownership or scheduling responsibility.
- Worker lineage and delegation lineage are separate: `WorkerRecord` tracks process delivery by key, while delegated status and optional parent IDs are persisted independently for session-tree nesting.
