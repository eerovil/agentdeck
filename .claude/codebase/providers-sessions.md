# Providers, Sessions, and Transcripts

This document covers provider-neutral session discovery, runtime ownership, transcript parsing, and Claude worker admission; read the sibling runtime and UI docs for transport or rendering details.

## Shared provider contract

- `src/agentdeck/models.py::Session` is the normalized record consumed by the web layer. Its URL/UI key is `<account_key>:<provider-native-session-id>`; `status` means process/source liveness, while `thinking` means an active turn.
- `src/agentdeck/providers/base.py::SessionProvider` separates authoritative `scan_sessions()` from cheap `sweep_liveness()`, transcript cursor/tail operations, usage polling, and optional control hooks.
- Gate UI behavior with `Session.capabilities` (`TRANSCRIPT`, `INJECT`, `STEER`, `INTERRUPT`, `INTERACT`, `DEEPLINK`), not provider-type checks. Ownership alone is insufficient when a runtime is unreachable.
- `src/agentdeck/collector.py::Collector` runs scan, liveness, and optional usage tasks per account. A full scan replaces only that account's slice in `AppState`; a liveness sweep mutates existing sessions and publishes `sessions` only when something changed. Failures are isolated so one provider loop does not stop collection.
- `src/agentdeck/state.py::visible_sessions()` hides ordinary idle sessions unless a provider sets `show_when_idle`. `session_tree()` builds one nesting level and omits orphaned native subagents rather than promoting contextless cards.
- Delegation has two durable paths: provider scans publish transcript-discovered child-to-parent full keys with `set_delegation_parents()`, while `mark_delegated_session()` stores the authoritative raw parent ID in the DB for lazy cross-provider resolution. `Session.is_delegated` also keeps background work out of `AssistantService._eligible_sessions()`.

## Claude Code discovery and ownership

- `src/agentdeck/providers/claude_code/provider.py::ClaudeCodeProvider.scan_sessions()` combines the newest 200 top-level project transcripts, live CLI-registry entries, history, and every ID in `ClaudeWorkerClient.owned_session_ids()`. Preserve the owned-ID union: a fresh worker may have no transcript yet, and an old owned chat may fall beyond the idle cap.
- Card metadata prefers transcript evidence, then history/registry fallbacks. In particular, transcript cwd keeps an idle chat attributable after its registry entry disappears; a transcript-less owned worker gets cwd from the runtime snapshot.
- Ordinary CLI sessions are transcript/deep-link sources. Only deck-owned workers gain send/steer/interrupt controls, and those capabilities are withheld whenever `ClaudeWorkerClient.available` is false even though its last ownership snapshot remains cached.
- Owned worker `live` and `turn_active` snapshot fields override the CLI registry, keep idle chats visible, and drive `status`, `thinking`, and controls. `worker_client.py::ClaudeWorkerClient` is only a Unix-socket facade and snapshot cache; the owning process is `worker.py::ClaudeWorkerHost`.
- Native Claude subagents live under `projects/<slug>/<parent-id>/subagents/*.jsonl`. The provider gives them `parent_session_key` and leaves their mtime-derived liveness to full scans; the cheap registry sweep intentionally skips them.
- Question state first uses the last event's normalized `AskUserQuestion`, then `transcripts.py::trailing_question()` for a natural-language question in the agent's final text. Do not infer waiting state from arbitrary question marks.

For process lifecycle, interaction normalization, and runtime/service ownership, continue in [owned-agent-control.md](owned-agent-control.md).

## Claude delivery bridge

- New chats derive `chat-<sha256-prefix>` and `delivery_id` from `action_context.current_client_action_id()`; sends to owned chats forward that same action ID. Without an action ID, new chats use a random key and delivery has no retry deduplication token.
- `ClaudeCodeProvider` maps a worker `delivery_id_conflict` to the UI-facing `client_action_conflict`. Receipt persistence, uncertain-write behavior, resume fallback, permission reuse, and orphan reconciliation belong in [owned-agent-control.md](owned-agent-control.md#delivery-idempotency).

## Codex discovery and ownership

- `src/agentdeck/providers/codex/provider.py::CodexProvider.scan_sessions()` parses the newest 200 date-partitioned rollouts. Non-owned liveness is only an mtime inference (`LIVE_WINDOW_S`); runtime-owned threads instead use app-server active-turn and pending-interaction state.
- A non-owned session is injectable only when it is idle, its cwd still exists, and `codex/inject.py::is_injectable_rollout()` confirms a completed `exec` turn. Owned threads route turns, compaction, steering, interruption, and interaction answers through `CodexRuntimeClient`.
- `codex/appserver.py::_recover_owned()` lists both `appServer` and `vscode` sources, then accepts only rollout paths inside the account's sessions tree whose first `session_meta` records `originator == "agentdeck"`; subagent sources are excluded. Do not equate the persisted source label with ownership.
- Spawned Codex rollouts reuse the parent `session_id`; their own child identity is `TranscriptMeta.agent_id`. Approval-review and other helper rollouts must be rejected before session-ID deduplication or a newer helper file can shadow the parent conversation.
- Transcript-discovered AgentDeck delegation markers are accepted only from tool-call output, not prompt text. `AppState`'s DB-backed delegation record remains the authoritative cross-provider fallback.

## Transcript parsing contract

- Both transcript parsers consume only newline-complete JSONL, retain a byte/sequence cursor, leave partial tails for the next read, and skip malformed lines with a counter rather than failing the session.
- Claude `transcripts.py::read_events()` removes a queued-user event once the matching durable user turn appears. It surfaces `AskUserQuestion` prompts/answers but hides generic tool-result bookkeeping and slash-command/meta echoes.
- Claude uses `_MAX_TEXT = 4_000` for card previews and `_MAX_RENDERED_TEXT = 200_000` for rendered conversation. Do not reuse preview extractors for transcript messages.
- Codex preserves conversation text while filtering known internal instruction/system envelopes and renderer metadata. Tool output and expandable invocation detail remain separately bounded by `_MAX_TOOL_OUTPUT = 4_000` and `_MAX_TOOL_DETAIL = 8_000`.
- Empty Codex reasoning events intentionally survive as hidden liveness heartbeats. `TranscriptMeta.last_agent_message` from `task_complete` is the canonical delegated-session result; `last_text` is only the last assistant item.

For SSE tailing, pagination, optimistic queued messages, and transcript rendering, continue in [web-ui-live-updates.md](web-ui-live-updates.md).

## Usage admission gotchas

- Claude and Codex usage pollers both atomically write `usage-<account.label>.json` through `claude_code.usage.write_shared_cache()`. The payload contains the full account key, but `ClaudeWorkerHost._read_published_usage()` selects by filename and does not validate that field.
- `AppConfig` requires uniqueness only within `(provider, label)`. Reusing one label for Codex and Claude therefore lets their pollers overwrite the same cache file and can make a Claude worker ceiling consume the wrong provider's percentage.
- An unreadable JSON file, absent numeric percentages, or a valid `fetched_at` older than 30 minutes means usage is unknown and does not block a spawn. Missing or malformed `fetched_at` currently skips the age check and still trusts numeric percentages; change `ClaudeWorkerHost._read_published_usage()` and its tests before relying on a different fail-open policy.
- Admission applies only to a spawn, revive, or fresh replacement. Steering an already-live worker remains allowed so work can finish; fresh replacement checks capacity and usage before terminating the healthy process. At capacity, `_evict_idle_worker()` may reclaim the least-recently-used idle process but never a worker with an active turn.
