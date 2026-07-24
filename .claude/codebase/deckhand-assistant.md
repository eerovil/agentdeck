# Deckhand Assistant

Scope: Read this before changing Deckhand attention triage, generated chat titles, dismissal persistence, status pills, push notifications, or their SSE publication.

See also [provider and session discovery](providers-sessions.md), [owned-agent control](owned-agent-control.md), and [runtime architecture](architecture-runtime.md).

## Service boundaries

- `src/agentdeck/app.py::create_app()` constructs `AssistantService` and `TitleService`; both use `[assistant]` settings from `src/agentdeck/config.py::AssistantConfig` and start after the collector.
- `src/agentdeck/assistant.py::AssistantService` answers which operator-owned sessions need attention. It owns the current `AssistantView`, git contexts, verdict cache, evidence signatures, handled state, persistence, and push dispatch.
- `src/agentdeck/triage.py` is the pure decision layer: `structured_trigger()`, `needs_llm()`, classifier prompts/parsing, verdict-to-card mapping, and card priority.
- `src/agentdeck/titles.py::TitleService` is a separate background service. Generated titles affect display only; `Session.title` remains the provider-native title and `Session.display_title` prefers `generated_title`.
- `src/agentdeck/deckhand_runner.py::run_codex_json()` is shared by triage and title generation. It runs ephemeral Codex with no approvals, ignored user config/rules, a read-only sandbox, an output schema, and a configured timeout.

## Deterministic-first attention triage

- `AssistantService._eligible_sessions()` starts from one `AppState.session_presentation()` snapshot. Delegated children with a visible parent report through that parent, while a top-level machine-started session has no other operator-facing handoff and remains eligible for Deckhand.
- `_triage_sessions()` keeps every session with a question or provider `PendingInteraction`, then fills the configured `max_sessions` window by most-recent activity.
- `refresh()` resolves `GitContext` first, computes a stable evidence signature, and calls `triage.structured_trigger()` before considering any model call.
- `structured_trigger()` returns the first applicable card, in this order: pending approval/question/browser action; `Session.question`; an open kanban issue whose final text says `claude:blocked`; a resting session with an open non-draft PR; a thinking session silent for `HANG_AFTER_S`.
- `triage.all_pulls_terminal()` suppresses transcript-based attention when all resolved PRs are merged or closed. `has_merged_pr()` separately drives the non-attention `merged` pill only for actually merged PRs.
- `triage.needs_llm()` admits only resting sessions where the agent spoke last and left non-empty prose. Active sessions, user-last sessions, structured cards, and terminal-PR sessions never need classification.
- `_dedupe_and_order()` deduplicates equal headlines across sessions and places active `waiting`/`stalled` cards before lower-priority `finished` cards.

## LLM fallback and failure policy

- `triage.classification_prompt()` asks for exactly `blocked` or `finished` from the bounded task and final-message excerpts. Both verdicts produce attention cards; neither means operator `done` or PR `merged`.
- `AssistantService.refresh()` reuses a verdict only while its evidence signature matches. Cold or changed sessions are classified with at most `CLASSIFY_CONCURRENCY = 8` subprocesses.
- `triage.parse_verdict()` coerces an invalid or missing status to `blocked`. `_classify()` also fails open to `blocked`; on a failed refresh, an existing cached verdict is preferred and the view is marked degraded.
- The fallback is intentionally read-only and text-only. Repository/PR facts belong in deterministic `GitContext` handling, not in classifier tool calls.

## Evidence and lifecycle signatures

- `AssistantService._evidence_signature()` includes native title, cwd, question, trimmed last prompt/text, worker type, issue status kind, pending-interaction identity/content, and sorted PR number/status/draft tuples.
- A failed GitHub lookup is incomplete context, not an empty PR set. Warm refreshes retain the last complete context; after a web restart the restored signature protects done markers until GitHub returns authoritative PR data.
- It deliberately excludes `thinking`, activity timestamps, and subagent churn. These transient values must not invalidate classifier caches, resurrect handled cards, or make pills flicker.
- Because `thinking` is excluded, `_carded_session_resumed()` explicitly wakes triage when a session backing a visible card resumes work, so a stale finished card drops promptly without needless reclassification.
- `_working_session_finished()` similarly bypasses the periodic throttle on the working-to-resting edge, so a completed turn surfaces its handoff immediately while ordinary activity events remain throttled.
- `_message_signature()` is narrower: question, last role, and trimmed last prompt/text. It is the identity for a dismissed question wait and changes on the next conversational turn rather than on git or polling noise.
- `src/agentdeck/titles.py::title_evidence_signature()` hashes only cleaned initial and latest user prompts. Assistant progress alone must not regenerate a semantic title.

## Delegated-session eligibility

- `src/agentdeck/state.py::AppState.mark_delegated_session()` records the child key (and optional raw parent id) in the DB, marks an already-scanned session delegated, and publishes `sessions`.
- `AppState.replace_account_sessions()` and `update_session()` reapply persisted delegation markers on rescans/restarts. See [owned-agent control](owned-agent-control.md) for how parents and worker children are established.
- `AssistantService.refresh()` prunes cached contexts/verdicts to Deckhand-eligible keys and removes persisted handled state when a previously handled session becomes an ineligible delegated child.
- `AssistantService.deckhand_statuses()` passes the same graph-aware eligibility decision into the pure pill resolver, so a child never gets its own stale status while a completed top-level delegation can show its handoff.

## Persistence, dismissal, and rendered status

- `src/agentdeck/db.py` stores the versioned assistant checkpoint (`view`, signatures, verdicts), `assistant_handled` rows, generated titles, and delegated-session markers. A valid checkpoint prevents unchanged cards from being recreated or pushed after restart.
- `AssistantService.handle()` treats a current `Session.question` specially: it persists `_WAITING_DONE_KIND` with `_message_signature()` and suppresses any corresponding waiting card until the question/message changes.
- Other insight dismissals persist the full evidence signature plus kind/headline/detail. `_apply_handled()` hides them while evidence is unchanged and restores them automatically after material evidence changes.
- `unhandle()` deletes persistence and immediately restores a still-current saved insight; it also requests a refresh. `handled_items` intentionally exposes only the most recent item as the sidebar undo surface while older rows remain persisted.
- `src/agentdeck/web/routes_actions.py::{handle_assistant_insight,unhandle_assistant_insight}` are same-origin-checked POST actions used by the sidebar, session details, and bottom transcript done/undo control.
- Attention cards, handled rows, session details, and push notifications identify the chat with its current `Session.display_title`; the insight headline remains supporting triage context rather than a second user-facing chat title.
- `src/agentdeck/web/render.py::session_deckhand_status()` applies low-to-high precedence: durable verdict, manual `done`, derived `merged`, live attention. `deckhand_pill()` additionally suppresses non-live stale status while a session is working and reads a pending question directly as `waiting`.

## Generated titles

- `TitleService._pending_sessions()` considers visible sessions with user intent. It may create the first title during an active turn, but waits for a later turn to settle before retitling an existing record.
- Unlike attention triage, title generation does not filter `Session.is_delegated`; visible delegated sessions may receive semantic display titles even though they never receive Deckhand attention cards or pills.
- Each refresh processes at most `_MAX_BATCH = 4`. `_bounded_context()` supplies the initial prompt, current title, and at most four recent non-subagent user/assistant events within a fixed character budget.
- `normalize_generated_title()` strips quoting/whitespace, caps at 80 characters, and removes model-supplied issue prefixes or kanban mode suffixes.
- `src/agentdeck/state.py::generated_display_title()` prefixes every generated title with stable project identity, reattaching `repo#number` and the native kanban mode suffix for issue sessions. Dashboard cards place the project prefix in their existing action row while every other surface uses the same complete display title. `AppState.set_generated_title()` persists the semantic title and evidence signature, updates the in-memory session, and publishes `sessions`.
- Failed title reads or model calls retain the native/current title; before saving a result, `TitleService.refresh()` rechecks the current session signature to discard stale in-flight output.

## Events, SSE, and push

- `AssistantService._watch_sessions()` subscribes to `sessions` and wakes the triage loop. TitleService has an equivalent session watcher.
- `AssistantService` publishes `assistant` while classification begins, after a changed/manual `_commit_view()`, and after handle/unhandle changes. `_commit_view()` also persists the checkpoint.
- `src/agentdeck/web/routes_sse.py` subscribes session pages to both `sessions` and `assistant`; an assistant change re-renders the session list, sidebar panel, per-session details, and `assistant-session-done` control.
- Dashboard/session-row pills derive from the same `AssistantService` view via `render.py::session_deckhand_status()`, so a new event source must publish `assistant` whenever its status inputs change.
- `_notify_new_insights()` defines push identity as `(session_key, headline)`. Explicit waiting cards notify immediately; resting-session handoffs wait for the next successful authoritative provider scan, then re-check effective parent activity so newly discovered child work can cancel a false alert. Pending cards stay out of the persisted checkpoint until dispatch, so a web restart retries rather than loses the notification. Unchanged, handled, or checkpoint-restored cards do not notify. `_dispatch_push()` sends through `PushService` off the event loop.
