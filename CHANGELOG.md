# Changelog

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
