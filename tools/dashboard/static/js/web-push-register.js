(function () {
  'use strict';
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register('/service-worker.js', {
    scope: '/',
    updateViaCache: 'none'
  }).catch(function (error) {
    console.warn('Web Push service worker registration failed:', error);
  });
})();
