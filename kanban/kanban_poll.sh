#!/usr/bin/env bash
# Token-free launcher for the AgentDeck kanban poller (run by a --user systemd
# timer). It does NO LLM inference: it lists `agent`-labelled issues and, for any
# not already in flight, dispatches a local AgentDeck worker to implement them.
# flock keeps overlapping timer firings from racing on the state file / worktrees.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # the repo checkout
KANBAN="$ROOT/kanban"
LOG="${AGENTDECK_KANBAN_LOG:-/tmp/agentdeck-kanban.log}"
LOCK="/tmp/agentdeck-kanban.lock"

# Prefer the repo venv's python; fall back to system python3 (poller.py is stdlib-only).
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[kanban] $(date -Is) another poll in progress — skipping" >>"$LOG"
  exit 0
fi

echo "[kanban] $(date -Is) poll start" >>"$LOG"
"$PY" "$KANBAN/poller.py" "$@" >>"$LOG" 2>&1
echo "[kanban] $(date -Is) poll done" >>"$LOG"
