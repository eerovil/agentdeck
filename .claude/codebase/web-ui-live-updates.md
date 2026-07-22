# Web UI Live Updates

This document covers server-rendered HTMX/SSE updates, client-state preservation, transcript following, and the mobile/PWA lifecycle.

For state producers and process boundaries, read [architecture-runtime.md](architecture-runtime.md). For discovery, transcript, and session-tree semantics, read [providers-sessions.md](providers-sessions.md); interaction ownership and answer delivery belong in [owned-agent-control.md](owned-agent-control.md).

## Rendering and event transport

- `src/agentdeck/web/render.py` is the shared fragment boundary for HTTP partials and SSE; keep one renderer/template for both paths.
- `src/agentdeck/events.py::EventBus` publishes coarse invalidation topics through bounded per-subscriber queues. A slow subscriber drops its oldest item.
- Treat bus messages as “render current state,” not ordered deltas. Dashboard and sidebar events carry complete replacement fragments, so coalescing intermediate invalidations is safe.
- `src/agentdeck/web/routes_sse.py::format_sse` serializes named HTML events; `events()` and `session_events()` disable caching and proxy buffering.
- `src/agentdeck/web/templates/base.html` enables the HTMX SSE extension on `<body>`; descendants select named streams with `sse-swap`.

## Dashboard stream

- `src/agentdeck/web/routes_sse.py::_stream` subscribes to `usage`, `sessions`, and `assistant`, primes all three on connection, coalesces queued topics, and otherwise emits heartbeat comments.
- Usage is refreshed at least every `USAGE_REFRESH_S`; an `assistant` invalidation also dirties `sessions` because Deckhand pills are rendered into session cards.
- `src/agentdeck/web/render.py::render_session_list` renders a supplied `SessionPresentation` through `partials/session_list.html`; relationship logic stays server-side.
- `src/agentdeck/state.py::AppState.session_presentation` returns one immutable, request-scoped view with one nesting level, omits orphan native subagents, drops finished native subagents, and keeps inactive delegated child sessions nested.
- `src/agentdeck/web/templates/dashboard.html` deliberately keeps search/filter inputs outside `#sessions`, which is replaced wholesale. It reapplies filtering after `htmx:afterSwap`.
- `src/agentdeck/web/templates/base.html` keeps card feeds, expanded cards, and subagent-open sets keyed outside replaced fragments, then reapplies them after each list swap. Usage expansion lives on `<body>` plus `localStorage` for the same reason.

## Session-detail stream

- `src/agentdeck/web/templates/session.html` replaces the body stream with one `/events/sessions/{session_key}` connection; transcript, status, usage, sidebar, controls, interaction, and Deckhand updates share that socket.
- `src/agentdeck/web/routes_sse.py::_session_stream` tails the provider transcript from a byte/sequence cursor. `transcript` uses `hx-swap="beforeend"`; other named fragments replace their target contents.
- Short-lived streaming state comes from recent transcript writes, while the busy marker follows the open turn/activity label so long tools remain visibly active.
- Status, subagent activity, tool activity, and composer controls are signature-gated. Sidebar and Deckhand fragments also react to `AppState` bus invalidations.
- `assistant-session-done` keeps `partials/session_done_button.html` synchronized with the sidebar without a polling control.

## Interactive-state invariant

Any background poll, HTMX response, SSE fragment, service-worker update, or reconnect must not wipe selected controls, unsent typed text, or focus. Layout and scroll state must also survive the real desktop/mobile lifecycle described in `AGENTS.md`.

- Never put an in-progress widget under a blind `hx-trigger="every Ns"` plus `outerHTML` replacement. Give it a named change topic and emit only when its underlying identity genuinely changes.
- `_session_stream` seeds `last_interaction_id` from the server-rendered pending interaction and emits `interaction` only when that ID changes. Unrelated status/tools traffic therefore cannot replace selected or typed answers.
- `session.html` owns the stable `#pending-interaction-slot`; `_session_stream` calls `render.py::render_pending_interaction` only for a new, cleared, or changed interaction identity.
- `src/agentdeck/web/static/interaction_feedback.js::immediateFeedback` copies the clicked submitter’s name/value into a hidden field before disabling buttons. `restoreFailure` removes the carrier and restores controls.
- The composer itself is not SSE-replaced; only `#composer-controls` swaps when interrupt capability changes. `base.html` persists each message draft in `sessionStorage`, clears only the exact submitted value, and preserves newer typing during an in-flight request.
- The service-worker `controllerchange` handler refuses to reload while a message draft is non-empty.

## Scroll, layout, and mobile lifecycle

- `src/agentdeck/web/static/session_bottom_follow.js::init` chooses the actual scroll root, follows an optimistic send through queued/status/transcript updates, and cancels when the reader deliberately scrolls away.
- Returning near the end resumes follow. HTMX and SSE transcript lifecycle handlers snapshot follow state around appends; `ResizeObserver` and `visualViewport.resize` cover later DOM and keyboard geometry changes.
- The same module’s `revealInteraction` listens to the real `interaction` SSE lifecycle and smoothly reveals a newly rendered answer form; clearing the slot does not scroll.
- `src/agentdeck/web/static/mobile_session_stack.js` keeps list and chat layers mounted, maps browser history/back and edge-swipe gestures between them, and therefore preserves the live list’s scroll position.
- Its `visualViewport` resize/scroll handler writes `--app-height`; `src/agentdeck/web/static/app.css` consumes it for the mobile stack and sticky composer. Prefer these real signals over timed keyboard/scroll retries.
- `base.html` closes tracked EventSources on `pagehide`, reloads a bfcache restore, and uses message age rather than `EventSource.readyState` to recover frozen/zombie PWA streams after visibility changes.
- `src/agentdeck/web/static/sw.js` is installability and static-shell caching only. Navigations, `/partials/*`, and `/events*` remain network-owned so dynamic HTML and SSE are never cached or buffered.

## Regression coverage

- `tests/test_inject.py::test_interaction_selection_survives_live_updates_e2e` drives shipped HTMX/SSE scripts: unrelated events preserve a radio selection and a genuine next interaction replaces it.
- `tests/test_inject.py::test_full_question_lifecycle_e2e_with_mock_llm` covers the real SSE route, reveal, survival, submission, and answer delivery; approval decisions and visual-viewport sizing have focused browser regressions nearby.
- `tests/test_web.py::test_session_bottom_follow_stops_on_manual_scroll_and_resumes_near_end` covers desktop and narrow-mobile scroll behavior; `test_message_draft_survives_reload_and_newer_text_is_not_cleared` covers draft ownership.
- `tests/test_web.py::test_mobile_chat_layers_over_live_list_and_back_and_swipe_are_instant` and session-tree tests protect mounted-layer and server-rendered nesting behavior.
- For new interactive UI, exercise the rendered page and actual HTMX/SSE events at desktop and the narrowest supported mobile width; assert input/focus survival, scroll cancellation, viewport fit, and the genuine change swap.
