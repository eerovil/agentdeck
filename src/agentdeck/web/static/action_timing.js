(function () {
  'use strict';

  var records = new Map();
  var relevantSseTargets = new Set([
    'session-status',
    'composer-controls',
    'pending-interaction',
    'inject-result',
    'sessions'
  ]);

  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function mark(record, phase) {
    if (!record || record.marks[phase] !== undefined) return;
    record.marks[phase] = performance.now();
    performance.mark('agentdeck:' + record.id + ':' + phase);
    document.dispatchEvent(new CustomEvent('agentdeck:action-timing', {
      detail: { actionId: record.id, action: record.action, phase: phase }
    }));
  }

  function actionPath(form) {
    return form.getAttribute('hx-post') || form.action || '';
  }

  function sessionKey(path) {
    var match = path.match(/^\/sessions\/([^/]+)\//);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function hiddenId(form, value) {
    var input = form.querySelector('input[name="client_action_id"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'client_action_id';
      form.appendChild(input);
    }
    input.value = value;
  }

  function prepare(form) {
    if (!form || !form.matches('form[data-agentdeck-action]')) return null;
    var current = form._agentdeckActionTiming;
    if (current && current.marks.response === undefined) return current;
    var path = actionPath(form);
    var record = {
      id: uuid(),
      action: form.dataset.agentdeckAction,
      path: path,
      sessionKey: sessionKey(path),
      marks: {},
      serverTiming: ''
    };
    form._agentdeckActionTiming = record;
    records.set(record.id, record);
    if (records.size > 100) records.delete(records.keys().next().value);
    hiddenId(form, record.id);
    mark(record, 'interaction');
    return record;
  }

  function formFromEvent(event) {
    var elt = event.detail && event.detail.elt;
    if (elt && elt.matches && elt.matches('form')) return elt;
    return elt && elt.closest ? elt.closest('form') : null;
  }

  document.addEventListener('pointerdown', function (event) {
    var button = event.target.closest('button[type="submit"], input[type="submit"]');
    if (button && button.form) prepare(button.form);
  }, true);

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    var form = event.target.form;
    if (form) prepare(form);
  }, true);

  document.addEventListener('submit', function (event) {
    prepare(event.target);
  }, true);

  document.body.addEventListener('htmx:configRequest', function (event) {
    var form = formFromEvent(event);
    var record = prepare(form);
    if (!record) return;
    event.detail.headers['X-AgentDeck-Action-ID'] = record.id;
    event.detail.headers['X-AgentDeck-Action'] = record.action;
    event.detail.parameters.client_action_id = record.id;
    mark(record, 'request_start');
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var form = formFromEvent(event);
    var record = form && form._agentdeckActionTiming;
    if (!record) return;
    var xhr = event.detail && event.detail.xhr;
    if (xhr && xhr.getResponseHeader) {
      record.serverTiming = xhr.getResponseHeader('Server-Timing') || '';
      record.status = xhr.status;
    }
    record.successful = Boolean(event.detail && event.detail.successful);
    mark(record, 'response');
  });

  document.body.addEventListener('htmx:sseMessage', function (event) {
    var target = event.target;
    if (!target || (!relevantSseTargets.has(target.id) && !target.matches('.transcript'))) {
      return;
    }
    records.forEach(function (record) {
      if (record.marks.request_start === undefined || record.marks.settled !== undefined) return;
      if (record.sessionKey &&
          decodeURIComponent(location.pathname).indexOf(record.sessionKey) === -1) return;
      mark(record, 'first_sse_state');
      var composerSettled = target.id === 'composer-controls' &&
        !target.querySelector('.stop-button');
      var interactionSettled = target.id === 'pending-interaction' && !target.textContent.trim();
      if (composerSettled || interactionSettled) {
        mark(record, 'settled');
      }
    });
  });

  window.AgentDeckActionTiming = {
    records: records,
    prepareForm: prepare,
    snapshot: function () {
      return Array.from(records.values()).map(function (record) {
        return JSON.parse(JSON.stringify(record));
      });
    },
    summary: function () {
      var grouped = {};
      records.forEach(function (record) {
        (grouped[record.action] = grouped[record.action] || []).push(record);
      });
      function percentiles(values) {
        if (!values.length) return null;
        values.sort(function (left, right) { return left - right; });
        function at(fraction) {
          return values[Math.min(values.length - 1, Math.ceil(values.length * fraction) - 1)];
        }
        return {p50: at(0.5), p95: at(0.95)};
      }
      function duration(items, start, end) {
        return percentiles(items.filter(function (item) {
          return item.marks[start] !== undefined && item.marks[end] !== undefined;
        }).map(function (item) { return item.marks[end] - item.marks[start]; }));
      }
      var result = {};
      Object.keys(grouped).forEach(function (action) {
        var items = grouped[action];
        result[action] = {
          samples: items.length,
          acknowledgement_ms: duration(items, 'interaction', 'acknowledged'),
          http_ms: duration(items, 'request_start', 'response'),
          sse_ms: duration(items, 'request_start', 'first_sse_state'),
          transcript_ms: duration(items, 'request_start', 'first_transcript'),
          settled_ms: duration(items, 'interaction', 'settled')
        };
      });
      return result;
    },
    mark: function (actionId, phase) { mark(records.get(actionId), phase); }
  };
})();
