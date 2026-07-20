# Review Skill Architecture

Scope: read this before changing `skills/rate-my-pr/` or its distribution through
`scripts/install-skills.sh`; it describes ownership boundaries and maintenance rules,
not the full review procedure.

## Source layout and ownership

- `skills/rate-my-pr/REVIEW.md` is the provider-neutral source of truth for what
  `rate-my-pr` does: arguments, diff acquisition, review lanes, filtering, synthesis,
  and report shape.
- `skills/rate-my-pr/{claude,codex}/SKILL.md` are adapters. They define only how
  `SPAWN_SUBREVIEWER(prompt, tier)` is implemented and how the `cheap` and `strong`
  tiers map to provider models; both then defer to `REVIEW.md` verbatim.
- Keep review policy in `REVIEW.md` and provider mechanics in the matching adapter.
  Do not duplicate the procedure into both `SKILL.md` files.
- Provider names, model pins, health checks, and delegation behavior may drift; update
  them in the adapter rather than making the shared procedure provider-aware.

## Shared review contract

- The skill is report-only: it does not edit, merge, run the application, or post a
  result unless the user separately authorizes posting.
- Correctness always runs. It fans out `cheap` reviewers according to effort, keeps
  findings with confidence at least 70, and deduplicates them by file and line.
- Architecture runs only at `high` or `max` (`ultra` aliases `max`). It uses one
  independent `strong` reviewer for each of five dimensions: right problem, right
  approach, right place, scope/blast radius, and use cases/UX.
- Architecture synthesis produces `Sound`, `Questionable`, or `Reconsider`. A
  non-`Sound` verdict must include a concrete alternative, including where and roughly
  how it should be built.
- PR mode obtains metadata and the PR diff through `gh`; working-diff mode reviews the
  default-branch merge-base through `HEAD` together with staged and unstaged changes.
  Both modes locate applicable `CLAUDE.md` and `AGENTS.md` guidance.
- Subreviewers cite specific files and lines, avoid lint/build signal and pre-existing
  issues, and distinguish verified bugs from uncertain concerns.

When changing a dimension, update its prompt, rating synthesis, report table, and both
provider descriptions together. The tracked contract currently has five dimensions;
do not document proposed dimensions that are absent from `REVIEW.md`.

## Provider adapters

- Claude implements each lane with the Agent tool. `cheap` is pinned to Sonnet with an
  `Explore` agent; `strong` is pinned to Opus with a `Plan` agent. Every call sets its
  model explicitly, and independent lanes run in parallel.
- Codex implements lanes as AgentDeck-owned child Codex chats via `agentdeck delegate`,
  keeping them visible and steerable. A local `/healthz` preflight is mandatory; on
  failure the workflow stops instead of falling back to raw `codex exec`.
- Codex model defaults live in the adapter and can be overridden with
  `RATE_MY_PR_CODEX_CHEAP_MODEL` and `RATE_MY_PR_CODEX_STRONG_MODEL`.
- Codex delegations use `workspace-write`, so every delegated brief must explicitly
  remain read-only. A child in `waiting` state is relayed to the user and then polled
  again after the user acts.

## Installation and maintenance

- `scripts/install-skills.sh` is the tracked distributor. For every directory under
  `skills/`, it installs each available provider adapter as the destination `SKILL.md`
  and places the same shared `REVIEW.md` beside it in a flat per-provider skill folder.
- Claude defaults to `~/.claude/skills` and `~/.claude2/skills`, overridable through
  space-separated `CLAUDE_SKILL_DIRS`; Codex defaults to `$CODEX_HOME/skills` or
  `~/.codex/skills`.
- Edit the tracked files in this repository, not installed copies. Re-run the installer
  to propagate changes; its layout is what makes each adapter's relative `REVIEW.md`
  reference resolve.
- A missing provider adapter or destination root is skipped, but a present adapter is
  expected to have a sibling shared `REVIEW.md`; preserve that shape for new skills.
