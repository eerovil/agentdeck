# AgentDeck Agent Guide

AgentDeck is a self-hosted, mobile-first FastAPI dashboard for monitoring and steering
Claude Code and Codex CLI sessions. It normalizes provider-specific transcripts into one
session model, renders a server-driven HTMX/SSE UI, and keeps runtime-owned agent processes
alive across web deployments. `CLAUDE.md` imports this file, so this is the shared root guide
for Claude and Codex; keep it lean and put subsystem depth in `.claude/codebase/`.

The dashboard has no authentication. Bind it only to localhost or a trusted private overlay;
anyone who can reach it can reach agent-control routes.

## Component map

| Area | Primary paths | Governing detail |
| --- | --- | --- |
| Web process and state | `src/agentdeck/{app,collector,state,events,db}.py` | Read [runtime architecture](.claude/codebase/architecture-runtime.md) before changing process ownership, state/event flow, socket control, or restarts. |
| Provider/session model | `src/agentdeck/providers/`, `src/agentdeck/models.py` | Read [providers and sessions](.claude/codebase/providers-sessions.md) before changing discovery, liveness, capabilities, transcripts, usage admission, or session trees. |
| Persistent control plane | `src/agentdeck/runtime.py`, provider worker/app-server clients | Read [owned agent control](.claude/codebase/owned-agent-control.md) before changing ownership, durable delivery, worker APIs, or restart continuation. |
| HTMX/SSE and mobile UI | `src/agentdeck/web/{routes_*,render}.py`, `templates/`, `static/` | Read [web UI live updates](.claude/codebase/web-ui-live-updates.md) before changing fragments, controls, transcript following, mobile layout, or PWA lifecycle. |
| Deckhand and titles | `src/agentdeck/{assistant,triage,titles,git_context,push}.py` | Read [Deckhand assistant](.claude/codebase/deckhand-assistant.md) before changing triage, dismissals, generated titles, pills, push, or assistant events. |
| Deploy and verification | `systemd/`, `scripts/staging-*.sh`, `tests/`, `.github/workflows/` | Read [deployment and testing](.claude/codebase/deployment-testing.md) before validating, staging, promoting, restarting, or verifying live. |
| Bundled review skill | `skills/rate-my-pr/`, `scripts/install-skills.sh` | Read [review skill architecture](.claude/codebase/skills-review.md) before changing the shared procedure, provider adapters, or installer. |

## Cross-cutting invariants

- Treat current code and exact live wire payloads as ground truth. Claude/Codex internals are
  undocumented and versioned outside this repo; parsers must skip/count unknown shapes rather
  than crash a provider scan.
- Gate UI behavior with normalized `Session.capabilities`, not provider-name conditionals.
  Transcript visibility is not ownership, and cached ownership is not runtime availability.
- Keep the restartable web process separate from the persistent runtime. A frontend deploy must
  not interrupt active turns, queues, pending interactions, or deck-owned Claude workers.
- Claude worker delivery is at-most-once by stable `delivery_id`. Never turn an uncertain Claude
  write into a fresh retry that can duplicate a user message or worker instruction; Codex client
  action IDs currently provide tracing, not an equivalent persisted replay cache.
- Any background poll, SSE swap, HTMX response, or service-worker lifecycle event must preserve
  selected controls, unsent text, focus, and deliberate scroll position. Interactive widgets
  update only on a real identity/change signal; never self-poll with `outerHTML` replacement.
- One queued user message appears optimistically, reconciles with its durable transcript event
  without duplication, and remains bottom-followed through queue/status/reply changes. Manual
  scrolling away cancels follow; mobile keyboard/viewport changes must not create gaps or jumps.
- Staging isolates code, DB, cache, and sockets, but shares real agent account directories.
  Injection and spawned workers can edit their cwd and consume the live account budget.

## Working style

- Implement clear requests directly after locating the governing behavior. When symptoms share
  an invariant, fix and test that invariant once instead of stacking narrow patches.
- Preserve unrelated work. Never stage, rewrite, or commit files outside the current request.
- Use `rg`/`rg --files`; batch independent reads and checks. Prefer one writer and use parallel
  agents for independent, read-heavy investigation or disjoint documentation files.
- Prefer reusable static JavaScript with stable entry points over growing inline template code.
  Browser tests must exercise rendered pages and the real HTMX/SSE lifecycle, not sliced source.
- For protocol/session defects, inspect the exact rollout, transcript, registry, or runtime
  snapshot before changing parsers, identity rules, ownership, or capability derivation.

## Fast validation tiers

- Documentation only: review rendered Markdown/text and run `git diff --check`; no application
  tests or service restart.
- Presentation-only template/CSS/JavaScript: run the focused web/browser scenario at desktop and
  the narrowest supported mobile width, then `git diff --check`. Run Ruff only if Python changed.
- Python/provider/queue/parser/runtime: run the closest focused tests, then the full suite once
  with `.venv/bin/pytest -q`; run Ruff on changed Python and `git diff --check`.
- If `.venv/bin` is unavailable, use the corresponding `uv run` command. Do not rerun a green
  test without a concrete reason; distinguish product failures from brittle fixtures/harnesses.

## Shipping and deployment

A completed AgentDeck application feature is committed, pushed to `master`, deployed, and
verified live. Perform that cycle once after acceptance criteria pass, not during iteration.

- Frontend/template/stylesheet/transcript-rendering changes restart only `agentdeck.service`.
  Restart `agentdeck-codex.service` only for persistent-runtime-owned changes and only after
  checking active chats and continuation safety. Never raw-restart it from an owned agent turn.
- Before a web restart, record both service PIDs. Afterward verify the web PID changed, the runtime
  PID stayed stable, `/healthz` is healthy, the exact affected page/workflow behaves correctly,
  logs explain uncertainty, and local `master`, `origin/master`, and the worktree are expected.
- A restart command returning is not deployment proof. Documentation-only guide changes are
  committed and pushed but require no service restart.

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical Matt Pocock triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This repo uses a single-context domain documentation layout. See `docs/agents/domain.md`.
