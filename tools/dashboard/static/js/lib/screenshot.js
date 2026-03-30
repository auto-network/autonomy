/**
 * Screenshot capture helpers — pure module, no DOM dependency for URL builder.
 *
 * Extracted from app.js for testability. Three functions:
 *   _screenshotUrl(revisionId, sessionName)  — build API URL
 *   _updateScreenshotStatus(revisionId, msg) — defensively update UI status
 *   _handleScreenshotResponse(revisionId, data) — process server response
 */
(function () {
  'use strict';

  /**
   * Build screenshot API URL, optionally including tmux_session.
   */
  function _screenshotUrl(revisionId, sessionName) {
    var url = '/api/design/' + revisionId + '/screenshot';
    if (sessionName) url += '?tmux_session=' + encodeURIComponent(sessionName);
    return url;
  }

  /**
   * Update screenshot status in the UI. Defensive — does not throw if
   * designPage or setScreenshotStatus is missing.
   */
  function _updateScreenshotStatus(revisionId, msg) {
    if (typeof window !== 'undefined' && window._designPage &&
        typeof window._designPage.setScreenshotStatus === 'function') {
      window._designPage.setScreenshotStatus(msg);
    } else if (typeof document !== 'undefined') {
      var el = document.getElementById('design-screenshot-status');
      if (el) el.textContent = msg;
    }
  }

  /**
   * Handle screenshot response — update status and trigger panel indicator.
   */
  function _handleScreenshotResponse(revisionId, data) {
    var now = new Date().toLocaleTimeString();
    if (data.injected) {
      _updateScreenshotStatus(revisionId, 'Screenshot injected ' + now);
      if (typeof document !== 'undefined') {
        var panelEl = document.getElementById('design-chat-panel');
        if (panelEl && typeof Alpine !== 'undefined') {
          var panelData = Alpine.$data(panelEl);
          if (panelData && panelData.showScreenshotInjected) panelData.showScreenshotInjected();
        }
      }
    } else {
      _updateScreenshotStatus(revisionId, 'Screenshot saved ' + now);
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
