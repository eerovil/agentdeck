(function () {
  'use strict';

  var NEAR_BOTTOM_PX = 80;

  function init() {
    var transcript = document.querySelector('.transcript');
    if (!transcript || transcript.dataset.bottomFollowReady) return;
    transcript.dataset.bottomFollowReady = 'true';

    var detail = document.querySelector('.session-detail');
    var followingSend = false;
    var cancelledSendFollow = false;
    var initialDone = false;
    var initialCancelled = false;
    var scrollGeneration = 0;
    var observedQueueIds = new Set();
    var observedActionIds = new Set();

    function scrollRoot() {
      if (detail && getComputedStyle(detail).overflowY === 'auto') return detail;
      return window;
    }

    function atBottom() {
      var root = scrollRoot();
      if (root === window) {
        return window.innerHeight + window.scrollY >=
          document.documentElement.scrollHeight - NEAR_BOTTOM_PX;
      }
      return root.clientHeight + root.scrollTop >= root.scrollHeight - NEAR_BOTTOM_PX;
    }

    function toBottom() {
      var root = scrollRoot();
      if (root === window) window.scrollTo(0, document.documentElement.scrollHeight);
      else root.scrollTo(0, root.scrollHeight);
    }

    function scheduleToBottom(generation) {
      requestAnimationFrame(function () {
        if (generation === scrollGeneration) toBottom();
      });
    }

    function cancelSendFollow() {
      initialCancelled = true;
      scrollGeneration += 1;
      if (followingSend) cancelledSendFollow = true;
      followingSend = false;
    }

    function onScroll() {
      if (!initialDone) initialCancelled = true;
      if (atBottom()) {
        // Once the reader deliberately returns to the end, resume following
        // the queued turn and its eventual assistant response.
        if (cancelledSendFollow) {
          followingSend = true;
          cancelledSendFollow = false;
        }
        return;
      }

      // A real position change is the source of truth. This also catches
      // scrollbar dragging and browser/keyboard scrolling that do not produce
      // wheel or touch events.
      scrollGeneration += 1;
      if (followingSend) cancelledSendFollow = true;
      followingSend = false;
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    if (detail) detail.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('wheel', function (event) {
      if (event.deltaY < 0) cancelSendFollow();
    }, { passive: true });
    window.addEventListener('keydown', function (event) {
      if (['PageUp', 'Home', 'ArrowUp'].includes(event.key) ||
          (event.key === ' ' && event.shiftKey)) {
        cancelSendFollow();
      }
    });

    function initialScroll() {
      if (initialDone) return;
      initialDone = true;
      var generation = scrollGeneration;
      requestAnimationFrame(function () {
        if (!initialCancelled && generation === scrollGeneration) toBottom();
      });
    }
    if (document.readyState === 'complete') initialScroll();
    else window.addEventListener('load', initialScroll, { once: true });

    function reconcilePendingMessages() {
      transcript.querySelectorAll('[data-observed-queue-id], [data-observed-action-id]')
        .forEach(function (durable) {
          var queueId = durable.dataset.observedQueueId;
          var actionId = durable.dataset.observedActionId;
          if (queueId) observedQueueIds.add(queueId);
          if (actionId && !observedActionIds.has(actionId)) {
            observedActionIds.add(actionId);
            if (window.AgentDeckActionTiming) {
              window.AgentDeckActionTiming.mark(actionId, 'first_transcript');
            }
          }
        });

      var actionRows = new Map();
      transcript.querySelectorAll('[data-pending-message]').forEach(function (pending) {
        var pendingQueueId = pending.dataset.queueId;
        var pendingActionId = pending.dataset.clientActionId;
        if ((pendingQueueId && observedQueueIds.has(pendingQueueId)) ||
            (pendingActionId && observedActionIds.has(pendingActionId))) {
          if (pendingActionId && window.AgentDeckActionTiming) {
            window.AgentDeckActionTiming.mark(pendingActionId, 'first_transcript');
          }
          pending.remove();
          return;
        }
        if (pendingActionId && actionRows.has(pendingActionId)) {
          pending.remove();
          return;
        }
        if (pendingActionId) actionRows.set(pendingActionId, pending);
      });
    }

    function beforeTranscriptSwap(event) {
      if (!event.target.matches('.transcript')) return;
      var composerFocused = document.activeElement &&
        document.activeElement.closest('.inject-form');
      event.target._agentdeckFollowSwap = {
        generation: scrollGeneration,
        enabled: followingSend || (atBottom() && !composerFocused),
      };
    }

    function afterTranscriptSwap(event) {
      if (!event.target.matches('.transcript')) return;
      reconcilePendingMessages();
      var swap = event.target._agentdeckFollowSwap;
      delete event.target._agentdeckFollowSwap;
      if (swap && swap.enabled) scheduleToBottom(swap.generation);
    }

    function revealInteraction(event) {
      // The pending-interaction widget arrives over its own `interaction` SSE
      // topic (see routes_sse), which fires htmx:sseMessage on the slot. When a
      // real prompt appears — a question or an approval, both carry the answer
      // form — bring it into view so the reader sees what needs answering, even
      // if they had scrolled up into history. The cleared state is an empty
      // section with no form, and must not scroll.
      if (!event.target || event.target.id !== 'pending-interaction-slot') return;
      var section = event.target.querySelector('#pending-interaction');
      if (!section || !section.querySelector('form[data-agentdeck-action="interaction"]')) {
        return;
      }
      // One rAF so the swapped-in widget has laid out before we measure/scroll.
      requestAnimationFrame(function () {
        section.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
      });
    }

    document.body.addEventListener('htmx:beforeSwap', beforeTranscriptSwap);
    document.body.addEventListener('htmx:afterSwap', afterTranscriptSwap);
    // The SSE extension uses its own lifecycle events instead of the ordinary
    // before/afterSwap pair.
    document.body.addEventListener('htmx:sseBeforeMessage', beforeTranscriptSwap);
    document.body.addEventListener('htmx:sseMessage', afterTranscriptSwap);
    document.body.addEventListener('htmx:sseMessage', revealInteraction);
    document.body.addEventListener('agentdeck:optimistic-send', function () {
      followingSend = true;
      cancelledSendFollow = false;
      scrollGeneration += 1;
      scheduleToBottom(scrollGeneration);
    });
    reconcilePendingMessages();

    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', function () {
        if (followingSend) scheduleToBottom(scrollGeneration);
      });
    }

    // Markdown enhancement and other real DOM changes can alter transcript
    // height after the HTMX/SSE swap itself. Keep following only while the
    // reader has not scrolled away.
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        reconcilePendingMessages();
        if (followingSend) scheduleToBottom(scrollGeneration);
      }).observe(transcript);
    }
  }

  window.AgentDeckSessionBottomFollow = { init: init };
  init();
})();
