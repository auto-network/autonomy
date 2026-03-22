// Global action sheet component — iOS-style slide-up tray.
// Registers Alpine.store('actionSheet') and exposes window.actionSheet API.
// Markup lives in base.html so it's available on every page.

(function () {

  document.addEventListener('alpine:init', function () {
    Alpine.store('actionSheet', {
      open: false,
      title: '',
      actions: [],
    });
  });

  window.actionSheet = {
    show: function (opts) {
      var store = Alpine.store('actionSheet');
      store.title = opts.title || '';
      store.actions = opts.actions || [];
      store.open = true;
      document.body.style.overflow = 'hidden';
    },

    dismiss: function () {
      var store = Alpine.store('actionSheet');
      store.open = false;
      store.title = '';
      store.actions = [];
      document.body.style.overflow = '';
      return new Promise(function (resolve) {
        setTimeout(resolve, 300);
      });
    },

    trigger: function (index) {
      var store = Alpine.store('actionSheet');
      var action = store.actions[index];
      if (action && typeof action.handler === 'function') {
        action.handler();
      }
      return window.actionSheet.dismiss();
    },

    isOpen: function () {
      var store = Alpine.store('actionSheet');
      return store ? store.open : false;
    },
  };

})();
