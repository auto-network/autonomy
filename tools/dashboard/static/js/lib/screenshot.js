/**
 * Screenshot capture helpers — pure module, no DOM dependency for URL builder.
 *
 * Extracted from app.js for testability. Three functions:
 *   _screenshotUrl(expId, sessionName)       — build API URL
 *   _updateScreenshotStatus(expId, msg)      — defensively update UI status
 *   _handleScreenshotResponse(expId, data)   — process server response
 */
(function () {
  'use strict';

  /**
   * Build screenshot API URL, optionally including tmux_session.
   */
  function _screenshotUrl(expId, sessionName) {
    var url = '/api/experiments/' + expId + '/screenshot';
    if (sessionName) url += '?tmux_session=' + encodeURIComponent(sessionName);
    return url;
  }

  /**
   * Update screenshot status in the UI. Defensive — does not throw if
   * experimentPage or setScreenshotStatus is missing.
   */
  function _updateScreenshotStatus(expId, msg) {
    if (typeof window !== 'undefined' && window._experimentPage &&
        typeof window._experimentPage.setScreenshotStatus === 'function') {
      window._experimentPage.setScreenshotStatus(msg);
    } else if (typeof document !== 'undefined') {
      var el = document.getElementById('exp-screenshot-status');
      if (el) el.textContent = msg;
    }
  }

  /**
   * Handle screenshot response — update status and trigger panel indicator.
   */
  function _handleScreenshotResponse(expId, data) {
    var now = new Date().toLocaleTimeString();
    if (data.injected) {
      _updateScreenshotStatus(expId, 'Screenshot injected ' + now);
      if (typeof document !== 'undefined') {
        var panelEl = document.getElementById('exp-chat-panel');
        if (panelEl && typeof Alpine !== 'undefined') {
          var panelData = Alpine.$data(panelEl);
          if (panelData && panelData.showScreenshotInjected) panelData.showScreenshotInjected();
        }
      }
    } else {
      _updateScreenshotStatus(expId, 'Screenshot saved ' + now);
    }
  }

  var Screenshot = {
    _screenshotUrl: _screenshotUrl,
    _updateScreenshotStatus: _updateScreenshotStatus,
    _handleScreenshotResponse: _handleScreenshotResponse,
  };

  // Dual export: browser (window) and Node (module.exports)
  if (typeof window !== 'undefined') {
    window.Screenshot = Screenshot;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = Screenshot;
  }
})();
