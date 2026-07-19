#!/usr/bin/env bash
# Bootstrap (or repair) the agentdeck STAGING / canary environment on this host.
#
# Staging runs the whole agentdeck stack a second time, isolated from the live
# instance, so new features and DB/schema changes can be exercised before they
# are promoted to master + the live services. Isolation:
#   - code:    a `staging`-branch git worktree at ~/agentdeck-staging (own .venv)
#   - config:  ~/.config/agentdeck/config-staging.toml (port 8757, own DB + cache)
#   - codex:   own control socket via AGENTDECK_CODEX_SOCKET (RuntimeDirectory)
#   - systemd: agentdeck-staging{,-codex}.service (this repo's systemd/ dir)
#   - phone:   tailscale serve on :8758 -> https://<tailnet-host>:8758 (PWA)
#
# Idempotent: safe to re-run to repair drift. Promote with staging-promote.sh.
set -euo pipefail

REPO="${AGENTDECK_REPO:-$HOME/agentdeck}"          # live checkout that owns .git
WT="${AGENTDECK_STAGING_DIR:-$HOME/agentdeck-staging}"
CONFIG="$HOME/.config/agentdeck/config-staging.toml"
UNIT_DIR="$HOME/.config/systemd/user"
TS_PORT="${AGENTDECK_STAGING_TS_PORT:-8758}"
WEB_PORT="8757"                                     # keep in sync with the config

echo "==> staging worktree: $WT"
if ! git -C "$REPO" worktree list --porcelain | grep -qx "worktree $WT"; then
  if git -C "$REPO" show-ref --quiet refs/heads/staging; then
    git -C "$REPO" worktree add "$WT" staging
  else
    git -C "$REPO" worktree add -b staging "$WT" master
  fi
else
  echo "    already present"
fi

echo "==> uv sync (staging .venv)"
( cd "$WT" && uv sync )

echo "==> config: $CONFIG"
if [[ ! -f "$CONFIG" ]]; then
  mkdir -p "$(dirname "$CONFIG")"
  cp "$WT/config.staging.example.toml" "$CONFIG"
  echo "    seeded from config.staging.example.toml"
else
  echo "    already present (left untouched)"
fi

echo "==> systemd units"
ln -sf "$WT/systemd/agentdeck-staging.service"       "$UNIT_DIR/"
ln -sf "$WT/systemd/agentdeck-staging-codex.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now agentdeck-staging-codex.service agentdeck-staging.service

echo "==> tailscale serve :$TS_PORT -> 127.0.0.1:$WEB_PORT"
tailscale serve --bg --https="$TS_PORT" "http://127.0.0.1:$WEB_PORT"

echo "==> health"
sleep 2
curl -fsS --max-time 6 "http://127.0.0.1:$WEB_PORT/healthz" && echo
echo "staging up: https://$(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/\.$//'):$TS_PORT/"
