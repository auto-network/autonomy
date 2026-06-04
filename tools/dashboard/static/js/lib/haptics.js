// haptics.js — a tiny, reusable web-haptics helper (#35).
//
// iOS Safari does NOT implement navigator.vibrate(), so the only reliable way to
// emit a haptic tap from the web is the Safari 17.4+ trick: an <input type=
// "checkbox" switch> control fires a haptic when toggled. We keep one hidden,
// off-screen switch and .click() it on demand. No-op everywhere it isn't
// supported (older iOS, desktop, no-DOM) — calling it is always safe.
//
// Public API: window.Autonomy.haptic()  — call from any user-gesture handler.
(function () {
  'use strict';

  var _switch = null;

  function _ensureSwitch() {
    if (_switch) return _switch;
    if (typeof document === 'undefined' || !document.body) return null;
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.setAttribute('switch', '');          // iOS 17.4+ native switch control
    input.setAttribute('aria-hidden', 'true');
    input.tabIndex = -1;
    // Off-screen but still interactive — display:none / visibility:hidden would
    // suppress the haptic, so hide it positionally instead.
    input.style.cssText =
      'position:fixed;top:-100px;left:-100px;width:1px;height:1px;' +
      'opacity:0;pointer-events:none;margin:0;border:0;padding:0;';
    document.body.appendChild(input);
    _switch = input;
    return _switch;
  }

  // Emit a single haptic tap. Best-effort: must be called from within a user
  // gesture for iOS to honour it; silently does nothing if unsupported.
  function haptic() {
    try {
      var el = _ensureSwitch();
      if (el && typeof el.click === 'function') el.click();
    } catch (_e) { /* unsupported → no-op */ }
  }

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.haptic = haptic;
})();
