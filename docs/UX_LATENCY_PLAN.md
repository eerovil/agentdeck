# AgentDeck interaction latency UX plan

## Goal

Make AgentDeck feel immediate even when an agent, provider, or remote model is slow. A user action
must produce visible feedback locally, be accepted or rejected quickly, and then reconcile with one
authoritative stream of runtime state. We cannot make model inference or a long-running tool finish
instantly, but AgentDeck should never leave the user wondering whether a tap worked.

This plan covers Send, queued follow-ups, Stop, approvals/questions, new-session creation, transcript
updates, and the session-list state derived from those actions. Deckhand model latency is tracked
separately because it is advisory rather than part of the direct chat-control loop.

## Experience contract and budgets

Measure each stage separately rather than treating "the agent responded" as one latency number.
Initial targets should hold on desktop and the installed mobile PWA over Tailscale:

| Stage | Target | User-visible result |
| --- | ---: | --- |
| Interaction to local acknowledgement | <= 100 ms | Pending message, Submitting, or Stopping appears |
| Action accepted/rejected by AgentDeck | p50 <= 250 ms, p95 <= 750 ms | Definite queued/accepted/error state |
| Runtime state change to browser | p95 <= 250 ms | Controls, interaction card, and activity state update |
| Transcript event available to browser | p95 <= 250 ms | New user/assistant/tool event appears |
| Idle UI after turn completion | p95 <= 500 ms | Spinner and Stop disappear; Send is ready |

Provider/model/tool execution time begins after action acceptance and is reported independently. A
slow agent is acceptable when the UI immediately says what it is doing and how long it has waited.

## Current latency path

The current implementation has several individually modest waits that can stack:

- The browser creates the pending transcript row only after the Send HTTP response returns.
- Codex control state is copied from the persistent runtime to the web process by a 1-second poll.
- A runtime action POST is followed by another full runtime-state GET before the web request returns.
- The session SSE stream tails transcripts and refreshes controls every 1.5 seconds.
- Pending interactions and injection status independently poll their fragments every 2 seconds.
- Claude/session discovery still has authoritative 15-second scans plus a shorter liveness sweep.
- Broad `sessions` events can re-render the full list even when one chat changed.

This creates visible gaps after Send, Stop, or Submit answer and makes the browser learn the same
state through several clocks. It also makes regressions difficult to localize because there is no
shared action identifier or end-to-end timing record.

## Target interaction flow

```text
tap / Enter
    -> local pending state (no network wait)
    -> POST action with client_action_id
    -> runtime returns receipt + thread revision
    -> runtime event stream updates web state
    -> per-session SSE updates the exact fragment
    -> transcript event reconciles the optimistic row
```

Every action has one stable ID and every thread-state update has a monotonic revision. Duplicate
requests are idempotent, late events cannot overwrite newer state, and optimistic UI is always
reconciled or rolled back from authoritative state.

## Work plan

### 1. Instrument before tuning

- Add a `client_action_id` to Send, Stop, interaction answers, steering, and new-session actions.
- Record browser marks for tap, request start, HTTP response, first matching SSE state, first matching
  transcript event, and settled UI.
- Add `Server-Timing` spans for form/upload parsing, queue admission, runtime RPC, runtime refresh,
  template rendering, and response serialization.
- Emit low-noise structured debug logs carrying action ID, session key, provider, state revision, and
  elapsed milliseconds across the web and persistent runtime processes.
- Build a diagnostic browser scenario that runs the real HTMX/SSE lifecycle with controllable
  network and provider delays. Report p50/p95 per stage rather than one total.

Deliverable: a baseline table for Send while idle, Send while busy, Stop, approval answer, question
answer, and turn completion on desktop and narrow mobile.

### 2. Make acknowledgement immediate

- On Send, insert an optimistic user row before the request leaves the browser. Give it the action ID
  and an explicit `sending` state; convert it to `queued` or `accepted` on the HTTP receipt.
- Preserve a recoverable copy of the draft until acceptance. On failure, restore the text and images
  without duplicating a row or losing text typed while the request was in flight.
- On Stop, immediately change the control to `Stopping...` and disable only Stop, not unrelated
  composer controls. Restore it if the request fails.
- On approval/question submission, immediately show `Submitting...` within the interaction card and
  prevent duplicate submission while preserving the selected answer on failure.
- Use stable, plain-language states: Sending, Queued behind active turn, Accepted, Stopping, Waiting
  for agent, and Failed with retry. Do not use a generic spinner for semantically different waits.

Deliverable: every interaction acknowledges within one animation frame, including under an
artificially delayed or failed server response.

### 3. Return action receipts instead of waiting for rediscovery

- Define an action receipt containing `client_action_id`, accepted/rejected state, queue position,
  thread ID, turn ID when known, and the current thread revision.
- Have runtime action endpoints return the updated state or revision produced by that action. The web
  client should not perform a second full `/state` GET before returning Stop/answer confirmation.
- Update web-side state immediately from the receipt, then let the event stream confirm it.
- Make action IDs idempotent in the persistent runtime so retries, reconnects, and double taps cannot
  create duplicate turns or answers.
- Keep errors typed (`stale_interaction`, `no_active_turn`, `runtime_unavailable`, `invalid_input`) so
  the UI can recover correctly instead of displaying an undifferentiated failure.

Deliverable: the action HTTP response is bounded by queue admission or the direct Codex RPC, not a
poll-and-refresh round trip.

### 4. Push runtime state to the web process

- Add a long-lived event stream over the existing Unix-socket boundary from the persistent Codex
  runtime to the web process. Events should include thread revision, active turn, interaction,
  ownership/capabilities, and action/queue status.
- Apply events to `AppState` once and publish targeted topics such as `session:<key>`,
  `interaction:<key>`, and `injection:<key>`.
- Keep a slow periodic snapshot only as reconnect/recovery protection. On reconnect, request changes
  since the last revision or one full snapshot if the history is unavailable.
- Eliminate the normal 1-second runtime polling dependency after event delivery is proven reliable.

Deliverable: an app-server notification changes the web-side session state without waiting for the
next poll.

### 5. Push exact UI fragments over the existing session SSE connection

- Add named session events for interaction, injection status, composer controls, queue state, and
  action errors. Reuse the page's one SSE connection rather than opening per-widget connections.
- Remove the 2-second `hx-get` polling loops from pending interactions and injection status after
  their SSE equivalents are covered.
- Trigger transcript tailing immediately from a runtime/transcript-changed event. Keep the current
  1.5-second tail only as a fallback until event-driven wakeups are reliable.
- For Claude transcript files, use filesystem notifications to wake a targeted incremental tail;
  retain periodic scans for recovery and non-evented metadata.
- Ensure fragment swaps preserve focus, typed drafts, selected approval answers, bottom-follow state,
  and the mobile visual viewport.

Deliverable: runtime and transcript changes reach the open chat without stacked polling intervals.

### 6. Make the queue durable and explicit

- Move the authoritative AgentDeck-owned Codex FIFO out of the restartable web process and into the
  persistent runtime (with a small SQLite journal if runtime restarts must also preserve it).
- Persist action ID, text/image references, queue position, state, and timestamps. Define image
  ownership and cleanup for queued, cancelled, completed, and restored actions.
- Restore pending rows and queue position after navigation or restart.
- Distinguish `Stop current turn` from `Cancel queued message` and `Cancel all queued`; never infer
  that Stop should silently discard follow-ups.

Deliverable: queue state is both fast to display and authoritative across web restarts.

### 7. Reduce broad rendering work

- Measure HTML size, render time, swap time, and layout cost for the dashboard and split-mode sidebar.
- Replace full session-list swaps with keyed per-card updates when one session changes. Use a batched
  list refresh only for ordering, insertion, deletion, or filter changes.
- Bound the initially rendered session list and load older results on demand while preserving search
  and direct navigation across all sessions.
- Avoid re-rendering usage, Deckhand, and unrelated session cards in response to a chat-control
  action unless their underlying state changed.

Deliverable: one active chat cannot make hundreds of unchanged cards repeatedly parse and lay out.

## Test strategy

- Unit tests: action receipt/state revision ordering, idempotency, rollback, queue transitions, and
  stale-event rejection.
- Runtime/web integration: real Unix-socket action plus pushed state; reconnect from a missed
  revision; web restart with active and queued turns.
- Browser regression: one scenario covering optimistic Send, HTTP receipt, queued state, SSE active
  state, transcript reconciliation, Stop, interaction submission, error rollback, and completion.
- Responsive checks: narrowest supported mobile width, virtual keyboard/`visualViewport`, desktop
  split mode, bounding boxes, duplicate controls, focus, and horizontal overflow.
- Fault injection: delayed action response, dropped SSE connection, duplicated/out-of-order event,
  runtime unavailable, stale interaction, and browser retry.
- Performance assertions: collect timing entries in tests and enforce generous regression ceilings;
  use live p50/p95 telemetry to tighten targets after rollout.

## Delivery sequence

1. Ship instrumentation alone and capture a live baseline.
2. Ship optimistic feedback and typed error recovery behind no protocol change.
3. Add action IDs/receipts and remove the post-action state refresh from the critical path.
4. Add runtime-to-web events, then interaction/injection/control SSE fragments.
5. Add event-driven transcript wakeups and demote polling to recovery.
6. Move the queue to its durable owner.
7. Optimize broad list rendering after measurements show its contribution.

Each step should be independently deployable and keep the existing polling path as a temporary
fallback. Remove a fallback only after reconnect, restart, and out-of-order-event tests pass live.

## Definition of done

- The experience budgets above hold in live desktop and mobile measurements.
- Every user action has immediate, specific feedback and an authoritative final state.
- No action can execute twice because of a retry, duplicate event, or double tap.
- A web restart does not lose an accepted or queued action.
- Normal Codex control and transcript updates are event-driven; polling is recovery-only.
- Slow provider/model execution remains clearly distinguishable from AgentDeck UI/control latency.
