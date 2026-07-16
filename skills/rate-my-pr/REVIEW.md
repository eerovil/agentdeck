# rate-my-pr — shared review procedure (provider-agnostic)

This file is the single source of truth for **what** the review does. It is
provider-neutral: it never says *how* to spawn a sub-reviewer, nor which model.
The `SKILL.md` that loaded you (Claude or Codex flavour) defines the one
primitive this procedure calls — `SPAWN_SUBREVIEWER(prompt, tier) -> findings` —
and maps each **tier** to a concrete model. `tier` is `cheap` (fast scan) or
`strong` (deep reasoning). Read that primitive's definition in your `SKILL.md`
first, then follow the steps below verbatim.

`rate-my-pr` is a **self-contained combined review**. It produces one report
with two parts:

1. **Correctness** — a bug/regression scan of the change (always runs).
2. **Architecture** — *"was this change a good idea in the first place?"* (runs
   only at `high`/`max` effort).

It never edits code and never merges. It only reports.

---

## Step 0 — Parse invocation

Arguments (all optional, any order): `[effort] [PR#]`

- **effort** ∈ `low | medium | high | max`. Default `medium`. `ultra` is an
  alias for `max` (this skill is local; there is no separate cloud engine).
  - `low` / `medium` → correctness pass only, few high-confidence findings.
  - `high` / `max` → correctness pass **plus** the architecture pass; broader
    coverage, may surface uncertain findings clearly labelled as such.
- **PR#** — a GitHub PR number (or `#123`). If given, review that PR. If
  omitted, review the **working diff** of the current branch.

## Step 1 — Acquire the diff and context

- **PR mode** (`PR#` given):
  - `gh pr view <n> --json title,body,baseRefName,headRefName,files,url`
  - `gh pr diff <n>`
- **Working-diff mode** (default):
  - base = merge-base with the repo's default branch
    (`git merge-base HEAD origin/HEAD` or the known default branch).
  - `git diff <base>...HEAD` **plus** uncommitted changes (`git diff` and
    `git diff --staged`). Review the union.
  - Read the branch's commit subjects for intent (`git log <base>..HEAD --oneline`).
- Locate governing context: the root `CLAUDE.md`/`AGENTS.md`, and any
  `CLAUDE.md`/`AGENTS.md` in the directories the diff touches. Note the file
  paths; sub-reviewers read them.

If the diff is empty, say so and stop.

## Step 2 — Correctness pass (always)

Spawn correctness sub-reviewers with `SPAWN_SUBREVIEWER(prompt, "cheap")` — the
scan is broad and shallow, so it uses the cheap tier. Scale the count to
effort: `low`→1, `medium`→2, `high`→3, `max`→4. Each gets the diff, the
CLAUDE.md/AGENTS.md paths, and this brief:

> Review ONLY the changed lines for correctness bugs and regressions. Focus on
> real bugs a senior engineer would block a merge on. Ignore nitpicks, style,
> and anything a linter/typechecker/CI would catch. Ignore pre-existing issues
> on unmodified lines. For each finding return: file:line, one-line description,
> why it's a real bug, and a confidence 0–100. Return nothing if the change is
> clean.

Keep only findings with confidence ≥ 70. Deduplicate across sub-reviewers by
file+line.

## Step 3 — Architecture pass (only when effort is `high` or `max`)

This is the point of the skill: judge whether the change **should exist at all**
and whether it was built the **right way**. Spawn **one sub-reviewer per
dimension** below with `SPAWN_SUBREVIEWER(prompt, "strong")` — design judgement
needs the strong tier. They run independently. Give each the diff, the branch/PR
intent from Step 1, the CLAUDE.md/AGENTS.md paths, and its dimension's question.
Each sub-reviewer must read enough of the surrounding codebase to judge, not just
the diff.

Dimensions:

1. **Right problem** — Is this solving a real, worthwhile problem? Or is it a
   non-issue, premature optimization, or scope invented by the author?
2. **Right approach** — Is the chosen approach sound, or is there a materially
   simpler/cleaner one? If a better approach exists, it must be sketched
   concretely.
3. **Right place** — Is the change in the correct layer/repo/module? (E.g. in a
   superproject-with-nested-repos, does dev-env wiring belong in the
   orchestration repo vs an app repo; does shared logic sit in the shared layer
   vs duplicated per-consumer.) Name the correct location if wrong.
4. **Scope & blast radius** — Scope creep, unnecessary coupling, migration/
   deploy/rollback risk, and whether it fits existing patterns and conventions.
5. **Use cases, UX & ease of use** — Does it actually serve the end user's real
   use case? Is it discoverable and easy to use/operate, or does it add friction,
   footguns, or surprising behavior?

Each dimension sub-reviewer returns:
- a **per-dimension rating**: `sound | questionable | reconsider`,
- 1–3 sentence justification grounded in specific files/lines, and
- when its rating is `questionable`/`reconsider`, a **concrete alternative**
  (which repo/layer/module, and the rough shape of the better change).

## Step 4 — Synthesize the verdict

Combine the dimension ratings into ONE overall **architecture verdict**:

- **Sound** — all (or all but one minor) dimensions are `sound`.
- **Questionable** — one or more dimensions are `questionable`, none demand a
  redo. Worth addressing but not a blocker.
- **Reconsider** — any dimension is `reconsider` (wrong problem, wrong approach,
  or wrong place). The change as-shaped should not ship without rework.

When the verdict is `Questionable` or `Reconsider`, the report **must** include a
concrete **Proposed alternative** (not just "this is wrong"): what to build
instead, where it belongs, and roughly how — synthesized from the dimension
sub-reviewers' alternatives.

## Step 5 — Output

Print one Markdown report. Do not edit files. Do not post anywhere unless the
user explicitly asked. Use this shape:

```
## rate-my-pr — <PR #n | working diff of <branch>>  (effort: <level>)

### Correctness (<N> issues)
1. <file:line> — <description> (<confidence>)
   <why it's a real bug>
...
(or: "No correctness issues found.")

### Architecture verdict: <Sound | Questionable | Reconsider>
<2–4 sentence overall judgement of whether this change was a good idea.>

| Dimension | Rating | Note |
|-----------|--------|------|
| Right problem     | … | … |
| Right approach    | … | … |
| Right place       | … | … |
| Scope & blast     | … | … |
| Use cases / UX    | … | … |

### Proposed alternative        ← only when verdict ≠ Sound
<what to build instead, where it belongs, rough shape.>
```

(Omit the Architecture sections entirely at `low`/`medium` effort, and say
"Architecture pass skipped — run at `high`/`max` for the design review.")

## Notes for every sub-reviewer

- Do not check build/lint/typecheck signal or run the app; assume CI covers it.
- Cite specific files and lines. Prefer evidence over assertion.
- Distinguish "verified real" from "suspected"; never inflate confidence.
