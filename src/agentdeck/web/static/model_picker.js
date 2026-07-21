// New-chat model picker: keep the model <select> in sync with the chosen
// account's provider. Options for other providers are removed (not just
// hidden — mobile Safari ignores `hidden` on <option>), and the whole field
// disappears when the selected provider offers no models. The picker lives in
// the create form only; model is fixed at spawn, so there is no in-chat variant.
(function () {
  function init() {
    var account = document.getElementById('new-chat-account');
    var model = document.getElementById('new-chat-model');
    var field = document.getElementById('new-chat-model-field');
    if (!account || !model || !field) return;

    // Snapshot every provider's options once; the live <select> is rebuilt from
    // this on each account change so switching provider can't strand a stale slug.
    var all = Array.prototype.map.call(
      model.querySelectorAll('option[data-provider]'),
      function (opt) {
        return {
          value: opt.value,
          label: opt.textContent,
          provider: opt.getAttribute('data-provider'),
        };
      }
    );

    function selectedProvider() {
      var opt = account.options[account.selectedIndex];
      return opt ? opt.getAttribute('data-provider') : '';
    }

    function rebuild() {
      var provider = selectedProvider();
      var previous = model.value;
      model.textContent = '';
      var def = document.createElement('option');
      def.value = '';
      def.textContent = 'Default (account)';
      model.appendChild(def);
      var keepPrevious = false;
      all.forEach(function (entry) {
        if (entry.provider !== provider) return;
        var opt = document.createElement('option');
        opt.value = entry.value;
        opt.textContent = entry.label;
        opt.setAttribute('data-provider', entry.provider);
        model.appendChild(opt);
        if (entry.value === previous) keepPrevious = true;
      });
      // Preserve the choice only when it still belongs to this provider.
      model.value = keepPrevious ? previous : '';
      field.hidden = model.options.length <= 1;
    }

    account.addEventListener('change', rebuild);
    rebuild();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
