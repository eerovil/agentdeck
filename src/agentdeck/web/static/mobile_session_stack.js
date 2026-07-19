(function () {
  'use strict';

  var mobile = window.matchMedia('(max-width: 999px)');
  var body = document.body;
  var layout = document.querySelector('.session-layout');
  var sidebar = document.querySelector('.session-sidebar');
  var detail = document.querySelector('.session-detail');
  var back = document.querySelector('a.back[href="/"]');
  if (!body.classList.contains('session-page') || !layout || !sidebar || !detail || !back) return;

  var initialized = false;
  var chatPath = location.pathname + location.search + location.hash;
  var STATE_KEY = 'agentdeckMobileLayer';

  function layerState(layer) {
    var state = Object.assign({}, history.state || {});
    state[STATE_KEY] = layer;
    state.agentdeckChatPath = chatPath;
    return state;
  }

  function setAccessibleLayer(showList) {
    detail.inert = showList;
    detail.setAttribute('aria-hidden', showList ? 'true' : 'false');
    sidebar.inert = !showList;
    sidebar.setAttribute('aria-hidden', showList ? 'false' : 'true');
    back.setAttribute('aria-hidden', showList ? 'true' : 'false');
  }

  function showLayer(layer) {
    var showList = layer === 'list';
    body.classList.toggle('mobile-list-open', showList);
    body.classList.remove('mobile-stack-dragging');
    detail.style.removeProperty('--mobile-chat-x');
    setAccessibleLayer(showList);
  }

  function initialize() {
    if (initialized || !mobile.matches) return;
    initialized = true;
    // The current document already contains both layers. Make the initial
    // history entry the list, then put the chat above it. Native Back can now
    // reveal the live list without a request, including after a direct deep link.
    history.replaceState(layerState('list'), '', '/');
    history.pushState(layerState('chat'), '', chatPath);
    // This is now an in-document history action, not an HTTP navigation.
    back.removeAttribute('data-agentdeck-action');
    body.classList.add('mobile-session-stack-ready');
    showLayer('chat');
  }

  function openList() {
    if (!initialized || !mobile.matches || body.classList.contains('mobile-list-open')) return;
    history.back();
  }

  document.addEventListener('click', function (event) {
    if (!mobile.matches || !event.target.closest('a.back[href="/"]')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    openList();
  }, true);

  window.addEventListener('popstate', function (event) {
    var layer = event.state && event.state[STATE_KEY];
    if (layer && mobile.matches) showLayer(layer);
  });

  // A deliberate swipe starting at the left edge drags the chat layer with the
  // finger. Vertical transcript scrolling wins unless horizontal movement is
  // clearly dominant, so ordinary reading never accidentally changes views.
  var drag = null;
  var EDGE_PX = 28;
  var COMMIT_RATIO = 0.28;

  function resetDrag() {
    drag = null;
    body.classList.remove('mobile-stack-dragging');
    detail.style.removeProperty('--mobile-chat-x');
  }

  detail.addEventListener('pointerdown', function (event) {
    if (!initialized || !mobile.matches || body.classList.contains('mobile-list-open') ||
        !event.isPrimary || event.clientX > EDGE_PX ||
        event.target.closest('input, textarea, select, button, a, [contenteditable]')) return;
    drag = {
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      horizontal: false,
    };
  });

  detail.addEventListener('pointermove', function (event) {
    if (!drag || event.pointerId !== drag.id) return;
    var dx = Math.max(0, event.clientX - drag.startX);
    var dy = Math.abs(event.clientY - drag.startY);
    if (!drag.horizontal) {
      if (dy > 10 && dy > dx) { resetDrag(); return; }
      if (dx < 8 || dx <= dy) return;
      drag.horizontal = true;
      body.classList.add('mobile-stack-dragging');
      try { detail.setPointerCapture(event.pointerId); } catch (_) {}
    }
    event.preventDefault();
    detail.style.setProperty('--mobile-chat-x', Math.min(dx, detail.clientWidth) + 'px');
  }, {passive: false});

  function finishDrag(event) {
    if (!drag || event.pointerId !== drag.id) return;
    var dx = Math.max(0, event.clientX - drag.startX);
    var shouldOpen = drag.horizontal && dx >= detail.clientWidth * COMMIT_RATIO;
    if (shouldOpen) {
      drag = null;
      openList();
      return;
    }
    resetDrag();
  }
  detail.addEventListener('pointerup', finishDrag);
  detail.addEventListener('pointercancel', resetDrag);

  // Keep the chat's usable height equal to the *visual* viewport so the sticky
  // composer — and its Send/Stop buttons — sits above the on-screen keyboard
  // instead of behind it. `dvh` doesn't shrink for the keyboard on iOS, so a
  // bottom-anchored composer otherwise falls under it (issue #17). One observer,
  // no timeouts, per the mobile-keyboard contract in AGENTS.md.
  var viewport = window.visualViewport;
  if (viewport) {
    var applyHeight = function () {
      if (mobile.matches) {
        document.documentElement.style.setProperty(
          '--app-height', Math.round(viewport.height) + 'px'
        );
      } else {
        document.documentElement.style.removeProperty('--app-height');
      }
    };
    viewport.addEventListener('resize', applyHeight);
    viewport.addEventListener('scroll', applyHeight);
    if (mobile.addEventListener) mobile.addEventListener('change', applyHeight);
    applyHeight();
  }

  initialize();
  if (mobile.addEventListener) mobile.addEventListener('change', initialize);
})();
