#!/usr/bin/env bash
# Promote the tested `staging` branch to the live agentdeck (canary -> prod).
#
# Flow (matches the manual git-pull + restart deploy):
#   1. refuse if the staging worktree has uncommitted changes
#   2. show what staging is ahead of master, then merge it (fast-forward)
#   3. uv sync the live checkout (deps may have moved)
#   4. restart the WEB app and health-check, then push master
#   5. only with --restart-runtime: restart the persistent runtime *last*, safely
#
# Ordering matters: the web restart and the push happen before any runtime
# restart, and NEVER `systemctl restart agentdeck-codex.service` directly. That
# unit uses KillMode=control-group, so when this script is run from inside an
# agent turn (the agent's bash lives in the runtime cgroup) a raw restart tears
# down this very script mid-promote — the merge lands but the push/restart never
# finish (issue #20). With --restart-runtime we instead use
# `agentdeck restart-runtime`, which detaches the restart and resumes the agent
# session afterward. A frontend-only promote needs no runtime restart at all.
#
# The live history DB is untouched by this script; if staging exercised a schema
# migration, the live app runs it on start against ~/.local/share/agentdeck/agentdeck.db.
#
# Usage: staging-promote.sh [--yes] [--push] [--no-ff] [--restart-runtime]
#   --yes              skip the confirmation prompt
#   --push             also push master to origin after a successful web restart
#   --no-ff            allow a merge commit instead of requiring fast-forward
#   --restart-runtime  also restart the persistent runtime (only when runtime-owned
#                      code changed); done last, after the push, via the safe path
set -euo pipefail

REPO="${AGENTDECK_REPO:-$HOME/agentdeck}"
WT="${AGENTDECK_STAGING_DIR:-$HOME/agentdeck-staging}"
ASSUME_YES=0; DO_PUSH=0; MERGE_MODE="--ff-only"; RESTART_RUNTIME=0
for a in "$@"; do case "$a" in
  --yes) ASSUME_YES=1 ;; --push) DO_PUSH=1 ;; --no-ff) MERGE_MODE="--no-ff" ;;
  --restart-runtime) RESTART_RUNTIME=1 ;;
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

git -C "$REPO" fetch origin master --quiet || true

AHEAD="$(git -C "$REPO" log --oneline master..staging)"
if [[ -z "$AHEAD" ]]; then
  # Nothing new to merge. A prior interrupted promote may have merged without
  # pushing — if so and --push is set, just finish the push rather than no-op.
  UNPUSHED="$(git -C "$REPO" log --oneline origin/master..master 2>/dev/null || true)"
  if [[ "$DO_PUSH" == 1 && -n "$UNPUSHED" ]]; then
    echo "master already contains staging but has unpushed commits — finishing the push:"
    echo "$UNPUSHED" | sed 's/^/  /'
    git -C "$REPO" push origin master
    echo "pushed."
  else
    echo "Nothing to promote: master already contains staging."
  fi
  exit 0
fi
echo "Promoting these staging commits to master:"; echo "$AHEAD" | sed 's/^/  /'
if [[ "$ASSUME_YES" != 1 ]]; then
  read -r -p "Merge into master and restart the LIVE agentdeck web? [y/N] " ans
  [[ "$ans" == [yY] ]] || { echo "aborted."; exit 1; }
fi

echo "==> merging staging -> master ($MERGE_MODE)"
git -C "$REPO" merge "$MERGE_MODE" staging

echo "==> uv sync (live)"
( cd "$REPO" && uv sync )

# Web only. agentdeck.service is a different cgroup from this script, so this is
# safe even when an agent runs the promote, and it refreshes the asset hash so
# browsers pull the new JS/CSS.
echo "==> restart web (agentdeck.service)"
systemctl --user restart agentdeck.service
sleep 2
curl -fsS --max-time 6 http://127.0.0.1:8756/healthz && echo

# Push before any runtime restart, so an interrupted runtime teardown can never
# lose the push.
if [[ "$DO_PUSH" == 1 ]]; then
  echo "==> push master -> origin"
  git -C "$REPO" push origin master
fi

# Runtime restart is opt-in and happens LAST. Never a raw `systemctl restart` of
# the runtime — see the header note (issue #20).
if [[ "$RESTART_RUNTIME" == 1 ]]; then
  if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
    echo "==> restart runtime via agentdeck restart-runtime (detached, resumes this session)"
    "$REPO/.venv/bin/agentdeck" restart-runtime \
      --then "Promotion finished — verify http://127.0.0.1:8756/healthz is OK and the change is live, then report."
    # This process may be torn down with the runtime cgroup before the next line;
    # the push and web deploy above are already done, so nothing is lost.
  else
    echo "==> restart runtime (agentdeck-codex.service)"
    systemctl --user restart agentdeck-codex.service
    sleep 2
    curl -fsS --max-time 6 http://127.0.0.1:8756/healthz && echo
  fi
fi
echo "promoted."
