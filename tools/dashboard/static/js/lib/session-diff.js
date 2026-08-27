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
    var re = /(\s+|\w+|[^\w\s])/g;
    var m;
    while ((m = re.exec(text)) !== null) {
      out.push(m[0]);
    }
    return out;
  }

  var PUNCTUATION_NORMALIZE_TABLE = {
    "\u2019": "'",
    "\u2018": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\ufe41": '"',
    "\ufe42": '"',
    "\xab": '"',
    "\xbb": '"',
  };

  function _normalizedToken(token) {
    if (token == null || token.length === 0) {
      return token;
    }
    return token.replace(/[\u2019\u2018\u201a\u201b\u201c\u201d\u201e\u201f\ufe41\ufe42\xab\xbb]/g, function (ch) {
      return PUNCTUATION_NORMALIZE_TABLE[ch] || ch;
    });
  }

  function normalizeCorrectionText(text) {
    return String(text == null ? '' : text)
      .replace(/\\r\\n/g, '\n')
      .replace(/\\n/g, '\n');
  }

  function _lcs(a, b) {
    var n = a.length, m = b.length;
    var dp = new Array(n + 1);
    for (var i = 0; i <= n; i++) {
      dp[i] = new Array(m + 1).fill(0);
    }
    for (var i = 1; i <= n; i++) {
      for (var j = 1; j <= m; j++) {
        if (_normalizedToken(a[i - 1]) === _normalizedToken(b[j - 1])) {
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
      if (_normalizedToken(a[i - 1]) === _normalizedToken(b[j - 1])) {
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
    // Ignore transport noise around the message (e.g. a trailing space).
    rawText = (rawText || '').trim();
    correctedText = (correctedText || '').trim();
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

  // ── Edit-tile unified diff (line-level LCS + word-level highlight) ──
  //
  // editDiffHTML(oldStr, newStr, {file_path, context}) → HTML string
  //
  // Produces a unified diff rendering for the Edit tile's Output tab:
  // each line is a block-level <span> tagged sc-diff-add / sc-diff-del /
  // sc-diff-ctx / sc-diff-hunk. Within replaced line pairs of equal
  // count, intra-line word runs are wrapped in <span class="sc-diff-word">
  // to highlight the actual changed tokens. Long unchanged runs collapse
  // to "@@ N unchanged lines @@" so the changed region stays in view.
  //
  // Output is HTML-escaped at every leaf — callers can drop it straight
  // into x-html without further sanitisation.

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function _wordTokens(s) {
    if (!s) return [];
    var out = [];
    var re = /([A-Za-z0-9_]+|[^A-Za-z0-9_])/g;
    var m;
    while ((m = re.exec(s)) !== null) out.push(m[0]);
    return out;
  }

  function _opcodes(a, b) {
    // SequenceMatcher.get_opcodes()-style output over two arrays.
    var n = a.length, m = b.length;
    var dp = _lcs(a, b);
    var raw = [];
    var i = n, j = m;
    while (i > 0 && j > 0) {
      if (a[i - 1] === b[j - 1]) { raw.push({ t: 'eq', i: i - 1, j: j - 1 }); i--; j--; }
      else if (dp[i - 1][j] >= dp[i][j - 1]) { raw.push({ t: 'del', i: i - 1, j: j }); i--; }
      else { raw.push({ t: 'ins', i: i, j: j - 1 }); j--; }
    }
    while (i > 0) { raw.push({ t: 'del', i: i - 1, j: 0 }); i--; }
    while (j > 0) { raw.push({ t: 'ins', i: 0, j: j - 1 }); j--; }
    raw.reverse();
    var chunks = [];
    var cur = null;
    for (var k = 0; k < raw.length; k++) {
      var op = raw[k];
      if (cur && cur.t === op.t) {
        if (op.t === 'eq') { cur.i2 = op.i + 1; cur.j2 = op.j + 1; }
        else if (op.t === 'del') { cur.i2 = op.i + 1; }
        else { cur.j2 = op.j + 1; }
      } else {
        if (cur) chunks.push(cur);
        if (op.t === 'eq') cur = { t: 'eq', i1: op.i, i2: op.i + 1, j1: op.j, j2: op.j + 1 };
        else if (op.t === 'del') cur = { t: 'del', i1: op.i, i2: op.i + 1, j1: op.j, j2: op.j };
        else cur = { t: 'ins', i1: op.i, i2: op.i, j1: op.j, j2: op.j + 1 };
      }
    }
    if (cur) chunks.push(cur);
    var out = [];
    for (var k = 0; k < chunks.length; k++) {
      var c = chunks[k], next = chunks[k + 1];
      if (c.t === 'del' && next && next.t === 'ins') {
        out.push({ tag: 'replace', i1: c.i1, i2: c.i2, j1: next.j1, j2: next.j2 });
        k++;
      } else if (c.t === 'ins' && next && next.t === 'del') {
        out.push({ tag: 'replace', i1: next.i1, i2: next.i2, j1: c.j1, j2: c.j2 });
        k++;
      } else {
        out.push({
          tag: c.t === 'eq' ? 'equal' : c.t === 'del' ? 'delete' : 'insert',
          i1: c.i1, i2: c.i2, j1: c.j1, j2: c.j2,
        });
      }
    }
    return out;
  }

  function _inlineHighlight(oldLine, newLine) {
    // Skip the O(NM) word-LCS on pathologically long lines — the highlight
    // value vanishes once lines wrap into the hundreds of tokens, and the
    // DP gets expensive in a tile that may render dozens of edits.
    var MAX = 400;
    if (oldLine.length > MAX || newLine.length > MAX) {
      return { oldHTML: _esc(oldLine), newHTML: _esc(newLine) };
    }
    var a = _wordTokens(oldLine);
    var b = _wordTokens(newLine);
    var ops = _opcodes(a, b);
    var oOut = [], nOut = [];
    for (var k = 0; k < ops.length; k++) {
      var op = ops[k];
      var aChunk = a.slice(op.i1, op.i2).join('');
      var bChunk = b.slice(op.j1, op.j2).join('');
      if (op.tag === 'equal') {
        oOut.push(_esc(aChunk));
        nOut.push(_esc(bChunk));
      } else if (op.tag === 'delete') {
        oOut.push('<span class="sc-diff-word">' + _esc(aChunk) + '</span>');
      } else if (op.tag === 'insert') {
        nOut.push('<span class="sc-diff-word">' + _esc(bChunk) + '</span>');
      } else if (op.tag === 'replace') {
        oOut.push('<span class="sc-diff-word">' + _esc(aChunk) + '</span>');
        nOut.push('<span class="sc-diff-word">' + _esc(bChunk) + '</span>');
      }
    }
    return { oldHTML: oOut.join(''), newHTML: nOut.join('') };
  }

  function _line(cls, marker, bodyHTML) {
    return '<span class="sc-diff-line ' + cls + '">'
      + '<span class="sc-diff-marker">' + marker + '</span>'
      + bodyHTML + '</span>';
  }

  function editDiffHTML(oldStr, newStr, opts) {
    opts = opts || {};
    var filePath = opts.file_path || '';
    var context = (opts.context != null) ? opts.context : 3;
    var old = oldStr || '';
    var neu = newStr || '';

    var parts = [];
    if (filePath) parts.push('<div class="sc-diff-file">' + _esc(filePath) + '</div>');

    var body = [];
    var i;

    if (!old && neu) {
      var nLines = neu.split('\n');
      for (i = 0; i < nLines.length; i++) body.push(_line('sc-diff-add', '+', _esc(nLines[i])));
    } else if (old && !neu) {
      var dLines = old.split('\n');
      for (i = 0; i < dLines.length; i++) body.push(_line('sc-diff-del', '−', _esc(dLines[i])));
    } else {
      var oldLines = old.split('\n');
      var newLines = neu.split('\n');
      var ops = _opcodes(oldLines, newLines);
      for (var k = 0; k < ops.length; k++) {
        var op = ops[k];
        if (op.tag === 'equal') {
          var block = oldLines.slice(op.i1, op.i2);
          var isFirst = k === 0;
          var isLast = k === ops.length - 1;
          if (block.length > context * 2 + 1 && !isFirst && !isLast) {
            for (var b = 0; b < context; b++) body.push(_line('sc-diff-ctx', ' ', _esc(block[b])));
            var hidden = block.length - context * 2;
            body.push(_line('sc-diff-hunk', ' ', '@@ ' + hidden + ' unchanged line' + (hidden !== 1 ? 's' : '') + ' @@'));
            for (var b = block.length - context; b < block.length; b++) body.push(_line('sc-diff-ctx', ' ', _esc(block[b])));
          } else if (isFirst && !isLast && block.length > context) {
            for (var b = block.length - context; b < block.length; b++) body.push(_line('sc-diff-ctx', ' ', _esc(block[b])));
          } else if (isLast && !isFirst && block.length > context) {
            for (var b = 0; b < context; b++) body.push(_line('sc-diff-ctx', ' ', _esc(block[b])));
          } else {
            for (var b = 0; b < block.length; b++) body.push(_line('sc-diff-ctx', ' ', _esc(block[b])));
          }
        } else if (op.tag === 'replace') {
          var o = oldLines.slice(op.i1, op.i2);
          var n = newLines.slice(op.j1, op.j2);
          if (o.length === n.length) {
            for (var z = 0; z < o.length; z++) {
              var hi = _inlineHighlight(o[z], n[z]);
              body.push(_line('sc-diff-del', '−', hi.oldHTML));
              body.push(_line('sc-diff-add', '+', hi.newHTML));
            }
          } else {
            for (var z = 0; z < o.length; z++) body.push(_line('sc-diff-del', '−', _esc(o[z])));
            for (var z = 0; z < n.length; z++) body.push(_line('sc-diff-add', '+', _esc(n[z])));
          }
        } else if (op.tag === 'delete') {
          for (var z = op.i1; z < op.i2; z++) body.push(_line('sc-diff-del', '−', _esc(oldLines[z])));
        } else if (op.tag === 'insert') {
          for (var z = op.j1; z < op.j2; z++) body.push(_line('sc-diff-add', '+', _esc(newLines[z])));
        }
      }
    }

    parts.push('<div class="sc-diff">' + body.join('') + '</div>');
    return parts.join('');
  }

  window.SessionDiff = {
    fragments: fragments,
    normalizeCorrectionText: normalizeCorrectionText,
    editDiffHTML: editDiffHTML,
  };
})();
