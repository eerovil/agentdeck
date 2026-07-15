# AgentDeck TODO

This file is the canonical lightweight backlog for AgentDeck. Add local product and bug
follow-ups here instead of creating GitHub issues unless external coordination is needed.

## Open

- [x] Make Deckhand refresh fast when chats and PR state are unchanged.
  - Measure where unchanged refresh time is spent: session collection, Git/PR context resolution,
    evidence comparison, or an unnecessary Luna invocation.
  - Verify automatic polling and manual Refresh separately; manual refresh should explain whether
    it is forcing a model run even when no material evidence changed.
  - Done when an unchanged refresh completes quickly without invoking Luna, real transcript or PR
    changes still trigger analysis, and timing plus invocation-count regressions cover both paths.

- [x] Fix duplicate, unresponsive Send buttons in the chat composer.
  - Reproduce the duplicate controls after live Stop/Send updates.
  - Check whether the `composer-controls` SSE fragment nests inside its own swap target,
    whether repeated events create duplicate IDs/forms, and whether Send and Stop retain the
    correct form association after swaps.
  - Done when exactly one Send button submits once, Stop appears and responds only during an
    interruptible turn, repeated SSE updates do not duplicate controls, and desktop plus narrow
    mobile regressions cover the behavior.

- [ ] Investigate Deckhand ignoring some newly created chats.
  - Check the 30-chat analysis window, initial visibility and ordering, collector timing, material
    evidence debounce, and whether blocking/question chats crowd ordinary new chats out.
  - Done when at least one missed-chat case is reproduced, the responsible collection/selection/
    debounce stage is identified, each newly eligible chat is evaluated once without re-running
    unchanged chats, and a regression covers the root cause.
