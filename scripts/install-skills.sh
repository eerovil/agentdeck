#!/usr/bin/env bash
# Install AgentDeck's provider-agnostic skills into each coding agent's skill dir.
#
# Single source lives in this repo under skills/<name>/ as:
#   skills/<name>/REVIEW.md        shared, provider-neutral procedure
#   skills/<name>/claude/SKILL.md  Claude flavour (Agent-tool fan-out)
#   skills/<name>/codex/SKILL.md   Codex flavour (agentdeck-delegate fan-out)
#
# Each provider gets a flat skill dir: its SKILL.md + the shared REVIEW.md
# alongside it, so the SKILL.md's "read REVIEW.md in this folder" resolves.
#
# Claude targets default to ~/.claude and ~/.claude2 (the outdoor two-login
# setup); override with CLAUDE_SKILL_DIRS (space-separated). Codex target
# defaults to ~/.codex; override with CODEX_HOME.
set -euo pipefail

SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../skills" && pwd)"

CLAUDE_SKILL_DIRS="${CLAUDE_SKILL_DIRS:-$HOME/.claude/skills $HOME/.claude2/skills}"
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"

install_flavour() {  # <skill> <flavour-subdir> <dest-skills-root>
  local skill="$1" flavour="$2" dest_root="$3"
  local src="$SRC_ROOT/$skill/$flavour/SKILL.md"
  [ -f "$src" ] || { echo "  skip: no $flavour flavour for $skill"; return; }
  [ -d "$dest_root" ] || { echo "  skip: $dest_root missing"; return; }
  local dest="$dest_root/$skill"
  mkdir -p "$dest"
  cp "$src" "$dest/SKILL.md"
  cp "$SRC_ROOT/$skill/REVIEW.md" "$dest/REVIEW.md"
  echo "  -> $dest"
}

for skill_dir in "$SRC_ROOT"/*/; do
  skill="$(basename "$skill_dir")"
  echo "$skill:"
  for cdir in $CLAUDE_SKILL_DIRS; do
    install_flavour "$skill" claude "$cdir"
  done
  install_flavour "$skill" codex "$CODEX_SKILLS_DIR"
done

echo "done."
