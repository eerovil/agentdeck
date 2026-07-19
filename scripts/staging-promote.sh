#!/usr/bin/env bash
# Promote the tested `staging` branch to the live agentdeck (canary -> prod).
#
# Flow (matches the manual git-pull + restart deploy):
#   1. refuse if the staging worktree has uncommitted changes
#   2. show what staging is ahead of master, then merge it (fast-forward)
#   3. uv sync the live checkout (deps may have moved)
#   4. restart the live services and health-check
#
# The live history DB is untouched by this script; if staging exercised a schema
# migration, the live app runs it on start against ~/.local/share/agentdeck/agentdeck.db.
#
# Usage: staging-promote.sh [--yes] [--push] [--no-ff]
#   --yes     skip the confirmation prompt
#   --push    also push master to origin after a successful restart
#   --no-ff   allow a merge commit instead of requiring fast-forward
set -euo pipefail

REPO="${AGENTDECK_REPO:-$HOME/agentdeck}"
WT="${AGENTDECK_STAGING_DIR:-$HOME/agentdeck-staging}"
ASSUME_YES=0; DO_PUSH=0; MERGE_MODE="--ff-only"
for a in "$@"; do case "$a" in
  --yes) ASSUME_YES=1 ;; --push) DO_PUSH=1 ;; --no-ff) MERGE_MODE="--no-ff" ;;
  *) echo "unknown arg: $a" >&2; exit 2 ;;
esac; done

# 1. staging must be committed
if [[ -n "$(git -C "$WT" status --porcelain)" ]]; then
  echo "ERROR: $WT has uncommitted changes — commit them on 'staging' first." >&2
  git -C "$WT" status --short >&2; exit 1
fi
# live checkout must be on a clean master
if [[ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" != "master" ]]; then
  echo "ERROR: $REPO is not on master." >&2; exit 1
fi
if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
  echo "ERROR: $REPO (live checkout) has uncommitted changes." >&2; exit 1
fi

AHEAD="$(git -C "$REPO" log --oneline master..staging)"
if [[ -z "$AHEAD" ]]; then echo "Nothing to promote: master already contains staging."; exit 0; fi
echo "Promoting these staging commits to master:"; echo "$AHEAD" | sed 's/^/  /'
if [[ "$ASSUME_YES" != 1 ]]; then
  read -r -p "Merge into master and restart the LIVE agentdeck? [y/N] " ans
  [[ "$ans" == [yY] ]] || { echo "aborted."; exit 1; }
fi

echo "==> merging staging -> master ($MERGE_MODE)"
git -C "$REPO" merge "$MERGE_MODE" staging

echo "==> uv sync (live)"
( cd "$REPO" && uv sync )

echo "==> restart live services"
systemctl --user restart agentdeck-codex.service agentdeck.service
sleep 2
curl -fsS --max-time 6 http://127.0.0.1:8756/healthz && echo

if [[ "$DO_PUSH" == 1 ]]; then
  echo "==> push master -> origin"
  git -C "$REPO" push origin master
fi
echo "promoted."
