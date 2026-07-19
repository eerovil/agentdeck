(function () {
  'use strict';

  // Active Deckhand insight cards normally open the chat. Instead, focus the
  // chat's row in the session list: scroll it into view and flash it. Handled
  // items keep their normal open-the-chat link. Modified clicks (cmd/ctrl/shift/
  // middle) still open the chat, so "open in new tab" keeps working.

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

  function flash(card) {
    card.classList.remove('dh-focus-flash');
    void card.offsetWidth; // restart the animation if it's still mid-flash
    card.classList.add('dh-focus-flash');
    window.setTimeout(function () {
      card.classList.remove('dh-focus-flash');
    }, 1400);
  }

  // Capture phase so this runs before action_timing's click handler; preventing
  // default also makes that handler's isPlainNavigation() bail (no open spinner).
  document.addEventListener('click', function (event) {
    var link = event.target.closest && event.target.closest('.assistant-insight-link');
    if (!link || !plainLeftClick(event)) return;
    event.preventDefault();
    var key = sessionKeyFromHref(link.href);
    if (!key) return;
    var card = cardFor(key);
    if (!card) return; // not in the list -> do nothing (per spec)
    card.scrollIntoView({behavior: 'smooth', block: 'center'});
    flash(card);
  }, true);
})();
