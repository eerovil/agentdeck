# AgentDeck TODO

This file is the canonical lightweight backlog for AgentDeck. Add local product and bug
follow-ups here instead of creating GitHub issues unless external coordination is needed.

## Open

- [ ] Investigate why visible Tilhi issue chats are absent from Deckhand.
  - Use the visible `tilhi#1632`, `tilhi#1633`, and `tilhi#1634` ALT/outdoor chats as reproduction
    cases; they report blocked terminal agent state, remain open for human action, and have no PR.
  - Trace whether they are excluded during session selection, omitted from the Luna update payload,
    omitted by Luna, deduplicated, or suppressed by Deckhand's post-processing filters.
  - Define the expected treatment for open-but-blocked issue chats and whether several similar
    autofix failures should appear individually or as one coordination item.
  - Done when the omission is explained, actionable Tilhi chats surface consistently, deliberate
    suppression is visible and understandable, and regressions cover the responsible stage.

- [x] Check that Luna reads only materially changed chats during Deckhand updates.
  - Audit the incremental prompt payload: the existing debounce may skip fully unchanged polls but
    still send the entire selected chat window when one chat changes.
  - Determine the minimum related-chat context needed for cross-chat coordination without
    repeatedly sending every unchanged transcript excerpt.
  - Done when update prompts contain only changed chats plus explicitly required related context,
    unchanged findings remain stable, and payload-content regressions cover transcript and PR
    changes.

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

- [x] Investigate Deckhand ignoring some newly created chats.
  - Check the 30-chat analysis window, initial visibility and ordering, collector timing, material
    evidence debounce, and whether blocking/question chats crowd ordinary new chats out.
  - Done when at least one missed-chat case is reproduced, the responsible collection/selection/
    debounce stage is identified, each newly eligible chat is evaluated once without re-running
    unchanged chats, and a regression covers the root cause.
