// Line-level diff counts — for the session viewer's Edit tile.
//
// IIFE pattern, not an ES module — session-renderer.js consumes via the
// global namespace. base.html MUST load this file before session-renderer.js.
//
// Used by lib/session-renderer.js to compute the +N / -N badges on the
// Edit-tool tile (single Edit) and the Agent-rollup tile (sum of nested
// Edits). The old code counted ALL lines of new_string and old_string,
// not the actual diff delta — every 10-line Edit showed +10 / -10
// whether or not the lines had changed. This module fixes that.
//
// Algorithm: split both inputs on \n, run LCS, count positions that are
// inserted (in new but not in the LCS) and deleted (in old but not in
// the LCS). Returns { added, removed }. Empty inputs treated as zero.
//
// Capped at 5000 lines per side. Beyond that, the LCS would dominate
// the renderer's frame budget; fall back to the cheap line-count delta
// so the tile still renders, just with less precision.

(function () {

  var MAX_LINES = 5000;

  function _split(s) {
    if (!s) return [];
    // Match _countLines semantics: a non-empty string with no \n is one
    // line; a string ending in \n splits into N+1 entries where the
    // last is empty (we treat it as a real line for diffing).
    return s.split('\n');
  }

  function _lcsLength(a, b) {
    // Length of the longest common subsequence of two line arrays.
    // O(n*m) time, O(min(n,m)) space (two-row rolling buffer).
    var n = a.length, m = b.length;
    if (n === 0 || m === 0) return 0;
    var prev = new Array(m + 1).fill(0);
    var curr = new Array(m + 1).fill(0);
    for (var i = 1; i <= n; i++) {
      for (var j = 1; j <= m; j++) {
        if (a[i - 1] === b[j - 1]) {
          curr[j] = prev[j - 1] + 1;
        } else {
          curr[j] = prev[j] >= curr[j - 1] ? prev[j] : curr[j - 1];
        }
      }
      // Swap rows.
      var tmp = prev;
      prev = curr;
      curr = tmp;
      // Zero the row we're about to write into.
      for (var k = 0; k <= m; k++) curr[k] = 0;
    }
    return prev[m];
  }

  function lineDiffCounts(oldStr, newStr) {
    var oldLines = _split(oldStr);
    var newLines = _split(newStr);

    // Bail to cheap behavior on very large inputs to protect the
    // renderer frame budget. The badge will overcount but at least the
    // tile renders without jank.
    if (oldLines.length > MAX_LINES || newLines.length > MAX_LINES) {
      return {
        added: newLines.length,
        removed: oldLines.length,
        estimate: true,
      };
    }

    var lcs = _lcsLength(oldLines, newLines);
    var added = newLines.length - lcs;
    var removed = oldLines.length - lcs;

    // Identical strings → both are 0; tile renders no badges.
    return { added: added, removed: removed, estimate: false };
  }

  window.AutonomyDiffLines = { lineDiffCounts: lineDiffCounts };

})();
