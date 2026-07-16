---
name: rate-my-pr
description: Combined review of the current diff (or a PR#) — a correctness bug scan plus, at high/max effort, an architect pass that judges whether the change was a good idea at all (right problem/approach/place, scope, UX) and proposes a concrete alternative when it wasn't. Use for "/rate-my-pr", "rate my PR", "was this change a good idea", "architect review my diff". Provider-agnostic skill; this is the Claude flavour.
---

# rate-my-pr (Claude flavour)

Review the current change for correctness **and** for whether it was a good idea
in the first place. Read-only: never edits code, never merges, never posts unless
the user explicitly asks.

## The one provider primitive

The shared procedure calls `SPAWN_SUBREVIEWER(prompt, tier) -> findings`. Under
Claude, realize it with the **Agent tool**:

- Spawn each sub-reviewer as its own agent, in parallel where the procedure runs
  a group concurrently (multiple `Agent` calls in one message).
- **Tier → model + agent type** (this is the per-lane pinning — edit here if
  model names drift):
  - `cheap`  → `model: "sonnet"`, `subagent_type: "Explore"` (correctness lane:
    read-only, locates and reads code).
  - `strong` → `model: "opus"`, `subagent_type: "Plan"` (architecture
    dimensions: design/trade-off reasoning).
- Pass the whole sub-reviewer brief as the agent `prompt`, and set `model` to the
  tier's model on every `Agent` call — do not rely on inherited defaults. The
  agent's returned text **is** its findings — feed it straight into synthesis.

Do not do the lanes yourself in the main thread when you can fan out; the value
is independent perspectives.

## Procedure

Read `REVIEW.md` in this skill folder and follow it verbatim. It defines the
arguments (`[effort] [PR#]`), effort gating (architecture pass only at
`high`/`max`), the correctness and architecture lanes, the verdict, and the exact
output format.

Invocation examples:
- `/rate-my-pr` — correctness scan of the working diff, medium effort.
- `/rate-my-pr high` — add the architecture pass on the working diff.
- `/rate-my-pr max 2646` — full review of PR #2646.
