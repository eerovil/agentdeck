# AgentDeck Agent Guide

## Working style

- Implement clear requests directly. Inspect enough code and live evidence to identify the
  governing behavior, then make one coherent change.
- Preserve unrelated work. The worktree may be dirty; never stage, rewrite, or commit files that
  are outside the current request.
- Use `rg` and `rg --files` for discovery. Batch independent reads and checks into as few tool
  round trips as practical.
- Keep user updates to meaningful milestones: start, an important discovery or changed assumption,
  validation, and deployment. Do not narrate every command.

## Optimize for the behavior, not the first symptom

Before editing an interactive UI bug, write down the invariant that should remain true across the
whole lifecycle. Inspect every event source and element that can affect it before patching.

For chat/composer work, treat this as the baseline contract:

- Transient state such as Working, Sending, queue status, and active controls must not change page
  height or cover message text.
- A queued user message appears in the transcript immediately and reconciles with the durable event
  without duplication.
- After a user sends, bottom-follow covers the optimistic message, queue/status changes, and the
  assistant reply delivered over SSE. Manual scrolling away cancels bottom-follow.
- Mobile keyboard and `visualViewport` changes must not leave a gap or cause repeated jumping.
- Prefer one scroll/layout controller and observation of real DOM or viewport changes over stacked
  timeouts and one-off event handlers.

Any element under a background poll, auto-refresh, or SSE swap must never re-render over in-progress
user input. For **every** new interactive UX element (form, multiple-choice/permission widget,
picker, inline editor), confirm before shipping that a refresh firing mid-interaction cannot wipe a
selected radio/checkbox, unsent typed text, or focus. Drive such an element off a real change signal
(an `sse-swap` topic pushed only when the underlying state actually changes) rather than a blind
timer, and re-render it only on a genuine change. In particular, do **not** give an interactive
element a self-polling `hx-trigger="every Ns"` with `hx-swap="outerHTML"`: the poll re-renders and
wipes input every tick, and an `outerHTML` self-replace does not cleanly hand the poll off to the
new node, so it keeps firing with stale request params. Add a browser regression that selects an
answer, lets the live update lifecycle run, and asserts the selection survives — and that a real
next-question still swaps the widget.

When several symptoms share an invariant, fix and test the invariant once instead of shipping a
sequence of narrow patches.

## Frontend structure and tests

- Prefer reusable JavaScript in a static module with stable entry points over adding more logic to a
  large inline template script.
- Browser tests should exercise the rendered page and the real HTMX/SSE lifecycle. Do not extract
  JavaScript by slicing source text at comments or incidental strings.
- Cover the complete user scenario in one regression where practical: send, queue, status update,
  SSE transcript reply, viewport resize, and manual-scroll cancellation.
- For responsive UI, verify the narrowest supported mobile width as well as desktop. Check bounding
  boxes, horizontal overflow, and unexpected layout-height changes, not only text presence.
- For Codex protocol/session bugs, inspect the exact live rollout or wire payload before generalizing
  a parser or identity rule.

## Fast validation tiers

Do not run every validation command after every internal edit. Finish the coherent patch, then use
the smallest tier that gives reliable evidence.

### Documentation only

- Review the rendered Markdown or relevant text.
- Run `git diff --check`.
- No application tests or service restart are required.

### Presentation-only template/CSS/JavaScript

- Run the focused web test or browser scenario that covers the changed behavior.
- Run Ruff only for changed Python files, if any.
- Run `git diff --check`.
- Run the full suite when shared rendering/contracts changed, the focused result is ambiguous, or a
  related UI batch is ready to ship. A tiny isolated style correction does not need repeated full
  suites during iteration.

### Python behavior, provider, queue, parser, or runtime

- Run the closest focused tests first.
- Then run the full suite once: `.venv/bin/pytest -q`.
- Run Ruff on changed Python files and `git diff --check`.

If `.venv/bin` is unavailable, use the corresponding `uv run` command. Do not rerun a green test
without a concrete reason. When a test fails, distinguish a product failure from a brittle fixture
or harness before changing production code.

## Shipping and deployment

A completed AgentDeck feature is committed, pushed to `master`, deployed, and verified live. Do the
commit/push/deploy cycle once after the feature's acceptance criteria pass, not after intermediate
attempts.

- Stage only files belonging to the current request.
- Push the resulting commit to `origin/master`.
- For frontend, template, stylesheet, transcript-rendering, and other web-process changes, restart
  only `agentdeck.service`. Do not restart `agentdeck-codex.service`.
- Restart `agentdeck-codex.service` only when code owned by the persistent runtime changed and the
  deployment requires it. Check active chats and plan continuity before doing so.
- Before a frontend restart, record both service PIDs. Afterward verify:
  - `agentdeck.service` has the expected new PID.
  - `agentdeck-codex.service` kept the same PID.
  - `http://127.0.0.1:8756/healthz` is healthy.
  - The affected live page contains or exhibits the new behavior.
  - Local `master`, `origin/master`, and the worktree are in the expected state.
- A restart command returning is not deployment proof. Check health, process identity, relevant
  live output, and logs when behavior is uncertain.

Documentation-only agent-guide changes are committed and pushed but do not require a service
restart because no running application code changed.

## Avoid recurring sources of delay

- Do not broaden a small request into unrelated polish, but do handle obvious states implied by the
  same behavior contract.
- Do not create a separate model/tool round trip for each read, test, or verification that can be
  safely batched.
- Do not repeatedly run the full suite, commit, or deploy while still iterating on one feature.
- Do not rely on synthetic HTMX events when production uses SSE-specific events.
- Do not use timed scroll retries when a real lifecycle event, `ResizeObserver`, or
  `visualViewport` signal can establish when layout changed.
- Keep the persistent Codex backend separate from frontend deployments; a UI deployment must not
  interrupt active turns.
