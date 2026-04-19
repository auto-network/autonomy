/**
 * Shared session stat formatters.
 *
 * Extracted from sessions.js so both the card partial (Alpine sessionsPage scope)
 * and the session viewer expanded header can render identical T3 stats.
 *
 * Each formatter accepts a session-like object and tolerates both shapes:
 *   - snake_case fields (s.entry_count, s.context_tokens, s.last_activity) — card path
 *   - camelCase fields (s.entryCount, s.contextTokens, s.lastActivity)     — store path
 */
(function () {

  function turnsStr(s) {
    var n = s.entry_count != null ? s.entry_count : s.entryCount;
    return n ? String(n) : '';
  }

  function ctxStr(s) {
    var t = s.context_tokens || s.contextTokens || 0;
    if (t >= 1000000) return (t / 1000000).toFixed(1) + 'M';
    if (t >= 1000) return Math.round(t / 1000) + 'K';
    return t ? String(t) : '';
  }

  function ctxWarn(s) {
    return (s.context_tokens || s.contextTokens || 0) > 700000;
  }

  function idleStr(s) {
    var epoch = s.last_activity != null ? s.last_activity : s.lastActivity;
    if (!epoch) return '';
    var secs = Math.round(Date.now() / 1000 - epoch);
    if (secs < 0) secs = 0;
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.round(secs / 60) + 'm';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h';
    return Math.floor(secs / 86400) + 'd';
  }

  function recencyColor(s) {
    var epoch = s.last_activity != null ? s.last_activity : s.lastActivity;
    if (!epoch) return 'gray';
    var secs = Math.round(Date.now() / 1000 - epoch);
    if (secs < 120) return 'green';
    if (secs < 600) return 'amber';
    return 'red';
  }

  window.SessionStats = {
    turnsStr: turnsStr,
    ctxStr: ctxStr,
    ctxWarn: ctxWarn,
    idleStr: idleStr,
    recencyColor: recencyColor,
  };
})();
