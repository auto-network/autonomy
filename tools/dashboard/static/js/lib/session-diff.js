// Session diff helper — computes inline {kind, text} fragments by
// comparing a raw user message to a corrected replacement string.
//
// Used by the turn-correction overlay (auto-edec1.4) to render the
// pending-state diff. The renderer iterates the fragment array and
// emits one span per fragment with .tc-frag-same / .tc-frag-del /
// .tc-frag-ins styling.
//
// Algorithm: word-level LCS via dynamic programming. Output preserves
// the original whitespace so spaces between words round-trip cleanly.

(function () {

  function _tokenize(text) {
    // Split on whitespace boundaries while keeping the whitespace as
    // its own token. Preserves rendering fidelity — adjacent same-text
    // tokens collapse into one fragment in the merge step.
    if (!text) return [];
    var out = [];
    var re = /(\s+|\S+)/g;
    var m;
    while ((m = re.exec(text)) !== null) {
      out.push(m[0]);
    }
    return out;
  }

  function _lcs(a, b) {
    var n = a.length, m = b.length;
    var dp = new Array(n + 1);
    for (var i = 0; i <= n; i++) {
      dp[i] = new Array(m + 1).fill(0);
    }
    for (var i = 1; i <= n; i++) {
      for (var j = 1; j <= m; j++) {
        if (a[i - 1] === b[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1] + 1;
        } else {
          dp[i][j] = dp[i - 1][j] >= dp[i][j - 1] ? dp[i - 1][j] : dp[i][j - 1];
        }
      }
    }
    return dp;
  }

  function _backtrack(a, b, dp) {
    var ops = [];
    var i = a.length, j = b.length;
    while (i > 0 && j > 0) {
      if (a[i - 1] === b[j - 1]) {
        ops.push({ kind: 'same', text: a[i - 1] });
        i--; j--;
      } else if (dp[i - 1][j] >= dp[i][j - 1]) {
        ops.push({ kind: 'delete', text: a[i - 1] });
        i--;
      } else {
        ops.push({ kind: 'insert', text: b[j - 1] });
        j--;
      }
    }
    while (i > 0) { ops.push({ kind: 'delete', text: a[i - 1] }); i--; }
    while (j > 0) { ops.push({ kind: 'insert', text: b[j - 1] }); j--; }
    ops.reverse();
    return ops;
  }

  function _merge(ops) {
    // Collapse adjacent same-kind ops so the renderer emits one span per run.
    var out = [];
    for (var i = 0; i < ops.length; i++) {
      var op = ops[i];
      var last = out.length ? out[out.length - 1] : null;
      if (last && last.kind === op.kind) {
        last.text += op.text;
      } else {
        out.push({ kind: op.kind, text: op.text });
      }
    }
    return out;
  }

  function fragments(rawText, correctedText) {
    if (rawText === correctedText) {
      return rawText ? [{ kind: 'same', text: rawText }] : [];
    }
    if (!rawText) return correctedText ? [{ kind: 'insert', text: correctedText }] : [];
    if (!correctedText) return [{ kind: 'delete', text: rawText }];
    var a = _tokenize(rawText);
    var b = _tokenize(correctedText);
    var dp = _lcs(a, b);
    return _merge(_backtrack(a, b, dp));
  }

  window.SessionDiff = { fragments: fragments };
})();
