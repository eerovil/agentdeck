# Changelog

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
