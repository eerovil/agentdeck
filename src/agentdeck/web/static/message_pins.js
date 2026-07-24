(function () {
  'use strict';

  function init() {
    var transcript = document.querySelector('.transcript[data-session-key]');
    var panel = document.getElementById('pinned-messages');
    if (!transcript || !panel || panel.dataset.pinControlsReady) return;
    panel.dataset.pinControlsReady = 'true';

    var sessionKey = transcript.dataset.sessionKey;
    var pinnedSeqs = new Set();

    function readPanel() {
      pinnedSeqs = new Set(
        Array.from(panel.querySelectorAll('[data-pinned-seq]'))
          .map(function (item) { return item.dataset.pinnedSeq; })
      );
      syncControls();
    }

    function syncControls() {
      document.querySelectorAll('[data-pin-toggle][data-pin-seq]').forEach(function (button) {
        var pinned = pinnedSeqs.has(button.dataset.pinSeq);
        button.setAttribute('aria-pressed', String(pinned));
        button.setAttribute('aria-label', (pinned ? 'Unpin' : 'Pin') +
          ' message #' + button.dataset.pinSeq);
        button.title = (pinned ? 'Unpin' : 'Pin') + ' message';
        button.textContent = '📌';
      });
    }

    async function toggle(button) {
      var seq = button.dataset.pinSeq;
      var shouldPin = !pinnedSeqs.has(seq);
      document.querySelectorAll('[data-pin-toggle][data-pin-seq="' + CSS.escape(seq) + '"]')
        .forEach(function (match) { match.disabled = true; });
      try {
        var response = await fetch(
          '/api/sessions/' + encodeURIComponent(sessionKey) + '/pins/' + encodeURIComponent(seq),
          { method: shouldPin ? 'PUT' : 'DELETE', headers: { Accept: 'application/json' } }
        );
        if (!response.ok) throw new Error('pin request failed: ' + response.status);
        if (shouldPin) pinnedSeqs.add(seq);
        else pinnedSeqs.delete(seq);
        syncControls();
      } catch (error) {
        button.title = 'Could not update pin';
      } finally {
        document.querySelectorAll('[data-pin-toggle][data-pin-seq="' + CSS.escape(seq) + '"]')
          .forEach(function (match) { match.disabled = false; });
      }
    }

    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-pin-toggle]');
      if (button) toggle(button);
    });
    document.body.addEventListener('htmx:afterSwap', syncControls);
    document.body.addEventListener('htmx:sseMessage', syncControls);
    new MutationObserver(readPanel).observe(panel, { childList: true });
    readPanel();
  }

  window.AgentDeckMessagePins = { init: init };
  init();
})();
