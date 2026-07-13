# Changelog

## v0.3.0 (unreleased)
- Added a **Codex CLI provider**. Local
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` chats now share the session
  cards, transcript detail view, live tail, model metadata, and token/context
  counters used by Claude Code. LIVE/IDLE is inferred from recent rollout
  writes because Codex exposes no process registry. Account usage bars are read
  through `codex app-server`: windows are matched by duration, including plans
  that currently return only a weekly limit.
- **Post-`/compact` cards no longer misreport.** A completed compaction (and
  other slash-command echoes) is bookkeeping, not a live turn, so cards used to
  read the *pre*-compact tail: a stale huge context size, a false "working"
  badge, and an old prompt/reply. Compaction is now a turn boundary — the
  context counter hides until the next real turn reveals the new (smaller) size,
  the open-turn probe reads idle, and the stale prompt/reply are dropped
  (`ai-title` still labels the card). Command/compaction lines are also filtered
  from the transcript view.
- Session cards show a **context-size** counter (`47k ctx`) in the meta row —
  how full the context window is now, taken from the input side of the latest
  usage block (input + cache-read + cache-create), not the cumulative token
  total. Read cheaply from the transcript tail (mtime-cached) and refreshed on
  the liveness sweep, so it ticks up live as a session works. Colour-coded like
  the usage bars — green under 500k, amber past the halfway mark of the 1M-token
  window, red in the near-full (800k+) auto-compaction zone.
- Cards recognise the **AskUserQuestion** (multiple-choice) tool: its prompt —
  which lives in the tool input, not a text block — is surfaced as the card's
  waiting-on-your-answer question, and an unanswered one reads as *waiting*
  rather than "Using tools". The transcript detail view renders the prompt too
  (as its own amber question block) instead of dropping the line as tool noise.
- Sessions show a pulsing **thinking** indicator (dot + label) in both the list
  and the detail header when the agent is actively streaming — a LIVE session
  whose transcript was written in the last ~25s — vs. live-but-waiting. Refreshed
  on the ~10s liveness sweep. On the session detail page the header's thinking
  state is driven directly off the live tail (server-pushed `status` events), so
  it reacts within ~1.5s instead of waiting for the sweep.
- Interactive streaming chat (opt-in, same `[inject]` kill-switch): a long-lived
  `claude -p --resume --input-format stream-json` child per session. Chat page
  with a composer, live bubbles over SSE, Stop button, and a replay buffer for
  reconnects. Same spawn-time interlocks as inject; idle chats are reaped and all
  children are torn down (process-group kill) on shutdown.
- Session cards now show a real title from the transcript's `ai-title` line
  (falling back to the first user prompt), plus a latest-prompt line — instead
  of a bare session id. Titles are mtime-cached so idle transcripts aren't
  re-parsed each scan.
- Transcript viewer collapses long tool-result output into a `<details>`
  accordion (one-line peek until expanded).

- Message injection into idle sessions: `POST /sessions/{key}/inject` runs
  `claude -p --resume` in the session's cwd, appending a turn (the collector's
  tail then shows the reply). Inject form on the detail page for idle sessions.
- Safety interlocks, re-checked at spawn time: refuses when the session's pid is
  live (single-writer JSONL), when the cwd is missing, or when the cwd is not
  trusted (`hasTrustDialogAccepted`); `[inject] enabled` kill-switch (default
  honoured — the live deploy ships it off until you opt in).
- `Capability.INJECT` surfaced on injectable sessions; the web layer keys the
  form off capabilities, never off provider type.

## v0.2.0 (unreleased)

- Session detail page (`/sessions/{key}`): transcript viewer with per-event
  role/tool/model rendering, subagent events inlined, and "load earlier"
  pagination for long transcripts.
- Live tail: per-session SSE stream (`/events/sessions/{key}`) that appends new
  transcript events from a byte cursor (idle sessions cost ~nothing) and pushes
  a status fragment when a session flips LIVE↔IDLE.
- Token totals (input/output/cache) summed from transcript `usage` blocks;
  last model + todos (`tasks/<sessionId>/`) shown on the detail page.
- Optional SQLite history (`[history] enabled`): usage snapshots + a
  sessions-seen ledger; a unicode sparkline of recent 5h usage in the header.

## v0.1.0 (unreleased)

- Read-only dashboard: limit bars (5h / 7d OAuth usage) + live/idle session list
  for any number of Claude Code config dirs ("accounts").
- Session discovery from `sessions/<pid>.json` registry files with `/proc`
  starttime pid-reuse guard; titles/last prompts from `history.jsonl`.
- OAuth usage poller with jitter, 429/5xx exponential backoff, 401 credential
  re-read, and an atomically-written shared usage cache under
  `$XDG_RUNTIME_DIR/agentdeck` for other tools to consume.
- HTMX + SSE web UI (`/`, `/healthz`, `/partials/*`, `/events`), mobile-first,
  sticky limit-bar header, stale-greying.
- Zero write paths: no message injection, no transcript parsing yet.
