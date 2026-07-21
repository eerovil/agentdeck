(function () {
  'use strict';

  // The first pointer activation on an active Deckhand insight focuses its row;
  // a second activation on the same insight opens the chat. Handled items,
  // keyboard activation, and modified clicks keep normal link navigation.

  var doubleTapWindowMs = 600;
  var lastTapKey = null;
  var lastTapAt = 0;
  var focusedCard = null;
  var focusObserver = null;
  var focusedCardHasBeenVisible = false;

  function plainLeftClick(event) {
    return event.button === 0 && !event.metaKey && !event.ctrlKey &&
      !event.shiftKey && !event.altKey;
  }

  function sessionKeyFromHref(href) {
    try {
      // href is the anchor's resolved .href (already absolute).
      var path = new URL(href).pathname;
      var match = path.match(/^\/sessions\/([^/]+)(?:\/|$)/);
      return match ? decodeURIComponent(match[1]) : null;
    } catch (_) {
      return null;
    }
  }

  function cardFor(key) {
    // data-session-key holds the raw key; match it literally inside the quoted
    // attribute selector (keys are provider:account:uuid — no quotes to escape).
    return document.querySelector('.session[data-session-key="' + key + '"]');
  }

  function clearFocus() {
    if (focusObserver) focusObserver.disconnect();
    focusObserver = null;
    if (focusedCard) focusedCard.classList.remove('dh-focused');
    focusedCard = null;
    focusedCardHasBeenVisible = false;
  }

  function focus(card) {
    clearFocus();
    focusedCard = card;
    focusedCard.classList.add('dh-focused');

    if (!window.IntersectionObserver) return;
    focusObserver = new IntersectionObserver(function (entries) {
      var entry = entries[0];
      if (!entry || entry.target !== focusedCard) return;
      if (entry.isIntersecting) {
        focusedCardHasBeenVisible = true;
      } else if (focusedCardHasBeenVisible) {
        clearFocus();
      }
    });
    focusObserver.observe(card);
  }

  // Capture phase so this runs before action_timing's click handler; preventing
  // default also makes that handler's isPlainNavigation() bail (no open spinner).
  document.addEventListener('click', function (event) {
    var sessionLink = event.target.closest && event.target.closest('a.session');
    var nestedAction = event.target.closest &&
      event.target.closest('.expand-btn, .cc-btn, .gh-btn');
    if (sessionLink === focusedCard && !nestedAction && plainLeftClick(event)) {
      clearFocus();
    }

    var link = event.target.closest && event.target.closest('.assistant-insight-link');
    if (!link || !plainLeftClick(event)) return;
    // Keyboard-generated clicks have detail=0 and should open directly.
    if (event.detail === 0) return;
    var key = sessionKeyFromHref(link.href);
    if (!key) return;
    var now = window.performance.now();
    if (key === lastTapKey && now - lastTapAt <= doubleTapWindowMs) {
      lastTapKey = null;
      lastTapAt = 0;
      clearFocus();
      return;
    }

    event.preventDefault();
    lastTapKey = key;
    lastTapAt = now;
    var card = cardFor(key);
    if (!card) return;
    card.scrollIntoView({behavior: 'smooth', block: 'center'});
    focus(card);
  }, true);

  // Session-list SSE swaps replace the focused row wholesale. Clear the old
  // state before HTMX removes it rather than carrying stale DOM ownership.
  document.addEventListener('htmx:beforeSwap', function (event) {
    var target = event.detail && event.detail.target;
    if (focusedCard && target &&
        (target === focusedCard || target.contains(focusedCard))) {
      clearFocus();
    }
  });
})();
