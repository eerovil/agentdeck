(function () {
  'use strict';

  function timing() { return window.AgentDeckActionTiming; }

  function statusLabel(row, value) {
    var label = row && row.querySelector('.message-state');
    if (label) label.textContent = value;
  }

  function optimisticMessage(form, record) {
    var transcript = document.querySelector('.transcript');
    var input = form.querySelector('textarea[name="message"]');
    if (!transcript || !input || !input.value.trim()) return null;
    var row = document.createElement('div');
    row.className = 'ev user pending-message optimistic-message sending';
    row.dataset.pendingMessage = '';
    row.dataset.clientActionId = record.id;

    var head = document.createElement('div');
    head.className = 'ev-head';
    var role = document.createElement('span');
    role.className = 'ev-role';
    role.textContent = 'user';
    var state = document.createElement('span');
    state.className = 'message-state';
    state.textContent = 'Sending';
    head.appendChild(role);
    head.appendChild(state);

    var text = document.createElement('div');
    text.className = 'ev-text';
    text.textContent = input.value.trim();
    row.appendChild(head);
    row.appendChild(text);
    var imageInput = form.querySelector('input[type="file"][name="images"]');
    if (imageInput && imageInput.files && imageInput.files.length) {
      var images = document.createElement('div');
      images.className = 'ev-images';
      images.setAttribute('aria-label', imageInput.files.length +
        (imageInput.files.length === 1 ? ' attached image' : ' attached images'));
      Array.from(imageInput.files).forEach(function (file, index) {
        var image = document.createElement('img');
        var url = URL.createObjectURL(file);
        image.className = 'ev-image';
        image.src = url;
        image.alt = 'Attached image ' + (index + 1);
        image.addEventListener('load', function () { URL.revokeObjectURL(url); }, {once: true});
        image.addEventListener('error', function () { URL.revokeObjectURL(url); }, {once: true});
        images.appendChild(image);
      });
      row.appendChild(images);
    }
    transcript.appendChild(row);
    form._agentdeckOptimisticMessage = row;
    document.body.dispatchEvent(new CustomEvent('agentdeck:optimistic-send'));
    return row;
  }

  function immediateFeedback(form, record, submitter) {
    if (!record || record.marks.acknowledged !== undefined) return;
    if (record.action === 'send' || record.action === 'steer') {
      optimisticMessage(form, record);
    } else if (record.action === 'stop') {
      var stop = submitter || document.querySelector('.stop-button');
      if (stop) {
        stop._agentdeckOriginalText = stop.textContent;
        stop.textContent = 'Stopping…';
        stop.disabled = true;
        form._agentdeckSubmitter = stop;
      }
    } else if (record.action === 'interaction') {
      var note = document.createElement('div');
      note.className = 'interaction-submitting';
      note.setAttribute('role', 'status');
      note.textContent = 'Submitting…';
      form.appendChild(note);
      // Disabling the submit button drops its name/value from the form htmx is
      // about to serialize, which would lose the chosen decision (accept /
      // decline / cancel for approvals; accept for questions). Carry the clicked
      // submitter's value in a hidden field first so htmx still sends it.
      if (submitter && submitter.name) {
        var carrier = document.createElement('input');
        carrier.type = 'hidden';
        carrier.name = submitter.name;
        carrier.value = submitter.value;
        form.appendChild(carrier);
        form._agentdeckDecisionCarrier = carrier;
      }
      form.querySelectorAll('button[type="submit"]').forEach(function (button) {
        button.disabled = true;
      });
      form._agentdeckSubmittingNote = note;
    } else if (record.action === 'new_session') {
      var result = document.querySelector('#new-session-result');
      if (result) {
        result.className = 'inject-result running optimistic-action-status';
        result.textContent = 'Starting chat…';
      }
    }
    timing().mark(record.id, 'acknowledged');
  }

  function restoreFailure(form, record) {
    if (record.action === 'send' || record.action === 'steer') {
      var row = form._agentdeckOptimisticMessage;
      if (row) {
        row.classList.remove('sending');
        row.classList.add('failed');
        statusLabel(row, 'Failed · retry');
      }
    } else if (record.action === 'stop') {
      var stop = form._agentdeckSubmitter;
      if (stop && stop.isConnected) {
        stop.textContent = stop._agentdeckOriginalText || 'Stop';
        stop.disabled = false;
      }
    } else if (record.action === 'interaction') {
      if (form._agentdeckSubmittingNote) form._agentdeckSubmittingNote.remove();
      if (form._agentdeckDecisionCarrier) {
        form._agentdeckDecisionCarrier.remove();
        form._agentdeckDecisionCarrier = null;
      }
      form.querySelectorAll('button[type="submit"]').forEach(function (button) {
        button.disabled = false;
      });
    } else if (record.action === 'new_session') {
      var result = document.querySelector('#new-session-result');
      if (result) {
        result.className = 'inject-result failed optimistic-action-status';
        result.textContent = 'Failed to start chat. Retry.';
      }
    }
  }

  function accept(form, record, xhr) {
    if (record.action !== 'send' && record.action !== 'steer') return;
    var row = form._agentdeckOptimisticMessage;
    if (!row) return;
    row.classList.remove('sending');
    row.classList.add('accepted');
    var receipt = xhr && xhr.getResponseHeader &&
      xhr.getResponseHeader('X-AgentDeck-Action-State');
    var queued = receipt === 'queued' ||
      (!receipt && xhr && /inject-result queued/.test(xhr.responseText || ''));
    statusLabel(row, queued ? 'Queued behind active turn' : 'Accepted');
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches('form[data-agentdeck-action]')) return;
    var record = timing().prepareForm(form);
    immediateFeedback(form, record, event.submitter);
  }, true);

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var form = event.detail && event.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-agentdeck-action]')) return;
    var record = form._agentdeckActionTiming;
    if (!record) return;
    if (event.detail.successful) accept(form, record, event.detail.xhr);
    else restoreFailure(form, record);
  });
})();
