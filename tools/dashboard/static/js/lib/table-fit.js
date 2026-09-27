/**
 * table-fit.js: content-aware column widths for rendered markdown tables.
 *
 * Browsers size an auto-layout table by handing out width in proportion to
 * each column's longest line. When a table does not fit, that squeezes the
 * columns whose content is short (numbers, labels, ids) down to a character
 * or two, while prose columns keep the room. Finding the minimum-height
 * layout for a width is NP-complete (Anderson and Sobti, 1999); this uses the
 * greedy widening heuristic of Hurst, Marriott and Moulder (2005): start every
 * column at its narrowest, then repeatedly widen the column that saves the
 * most height per pixel of width, up to the available width for free and
 * beyond it only while a pixel of width still buys `lambda` pixels of height
 * (the table then scrolls sideways).
 *
 * On a narrow screen, a prose-heavy table that would still need several
 * screens of sideways scrolling is offered as stacked cards (one card per row,
 * the first column as its title, the others as labelled lines), with a
 * Table / Cards toggle.
 *
 * `chooseWidths` is pure and unit-tested (tests/test_table_fit.js).
 * `attach` is the DOM side: markdown.js calls it for every table it renders.
 */
(function (root) {
  'use strict';

  var STEP = 4;             // width granularity in px
  var MAX_CELLS = 800;      // larger tables keep the CSS-only layout
  var TIME_BUDGET_MS = 80;  // stop widening once this is spent
  var NARROW_PX = 560;      // below this, prose tables may be shown as cards

  function sum(a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s; }

  /**
   * Choose content widths (px, excluding padding) for each column.
   *
   * opts.nrow, opts.ncol
   * opts.min[c], opts.max[c]  narrowest and widest useful content width
   * opts.extra[c]             non-content width of the column (padding, border)
   * opts.heightAt(r, c, w)    height of cell (r, c) at content width w
   * opts.budget               outer width available without scrolling
   * opts.lambda               px of height a px of width must save past budget
   * opts.whole                columns whose widest cell is at most this many
   *                           px start at full width: a number with its unit
   *                           or a short label never wraps. Height alone
   *                           cannot see this, since a row is as tall as its
   *                           tallest cell and a wrapped short cell beside a
   *                           tall prose cell costs no height.
   * opts.deadline             optional clock value after which widening stops
   * opts.now                  optional clock (defaults to Date.now)
   */
  function chooseWidths(opts) {
    var nrow = opts.nrow, ncol = opts.ncol;
    var min = opts.min, max = opts.max, extra = opts.extra || [];
    var heightAt = opts.heightAt, budget = opts.budget;
    var lambda = opts.lambda == null ? 1 : opts.lambda;
    var step = opts.step || STEP;
    var now = opts.now || Date.now;
    var w = [], c, r;
    var whole = opts.whole || 0;
    for (c = 0; c < ncol; c++) w[c] = max[c] <= whole ? max[c] : min[c];
    var ex = 0;
    for (c = 0; c < ncol; c++) ex += extra[c] || 0;

    // h[r][c]: current height of each cell; rowH[r]: current row height.
    var h = [], rowH = [];
    for (r = 0; r < nrow; r++) {
      h[r] = [];
      for (c = 0; c < ncol; c++) h[r][c] = heightAt(r, c, w[c]);
      rowH[r] = Math.max.apply(null, h[r]);
    }
    function otherMax(row, col) {
      var m = 0;
      for (var d = 0; d < ncol; d++) if (d !== col && h[row][d] > m) m = h[row][d];
      return m;
    }

    for (var guard = 0; guard < 10000; guard++) {
      if (opts.deadline && now() > opts.deadline) break;
      var best = null;
      for (c = 0; c < ncol; c++) {
        if (w[c] >= max[c]) continue;
        // The next width at which this column makes the table shorter.
        for (var nw = Math.min(max[c], w[c] + step); ; nw = Math.min(max[c], nw + step)) {
          var saved = 0;
          for (r = 0; r < nrow; r++) {
            var nh = Math.max(otherMax(r, c), heightAt(r, c, nw));
            saved += rowH[r] - nh;
          }
          if (saved > 0) {
            var gain = saved / (nw - w[c]);
            if (!best || gain > best.gain) best = { c: c, nw: nw, gain: gain };
            break;
          }
          if (nw >= max[c]) break;
        }
      }
      if (!best) break;
      var widthAfter = sum(w) + ex + (best.nw - w[best.c]);
      if (widthAfter > budget && best.gain < lambda) break;
      w[best.c] = best.nw;
      for (r = 0; r < nrow; r++) {
        h[r][best.c] = heightAt(r, best.c, best.nw);
        rowH[r] = Math.max.apply(null, h[r]);
      }
    }

    // Width left inside the budget goes to the columns that can still use it,
    // in proportion to how much more they could take, so a table that fits
    // fills the column instead of leaving a ragged gap.
    var spare = budget - (sum(w) + ex);
    if (spare > 0) {
      var room = [], total = 0;
      for (c = 0; c < ncol; c++) { room[c] = Math.max(0, max[c] - w[c]); total += room[c]; }
      if (total > 0) {
        var give = Math.min(spare, total);
        for (c = 0; c < ncol; c++) w[c] += Math.floor(give * room[c] / total);
      }
    }
    return w;
  }

  function tableHeight(opts, w) {
    var H = 0;
    for (var r = 0; r < opts.nrow; r++) {
      var m = 0;
      for (var c = 0; c < opts.ncol; c++) m = Math.max(m, opts.heightAt(r, c, w[c]));
      H += m;
    }
    return H;
  }

  /**
   * The layout to use: fitted to the budget, unless letting the table scroll
   * sideways is clearly worth it. A table a few pixels wider than the screen
   * trades one line of height for a sideways scroll, which is worse, so the
   * wider layout must be at least `minWider` times the budget and at most
   * `maxTaller` times the fitted layout's height.
   */
  function chooseLayout(opts) {
    var fitted = chooseWidths(Object.assign({}, opts, { lambda: Infinity }));
    var wide = chooseWidths(Object.assign({}, opts, { lambda: opts.lambda == null ? 1 : opts.lambda }));
    var ex = 0;
    for (var c = 0; c < opts.ncol; c++) ex += (opts.extra || [])[c] || 0;
    var wideOuter = sum(wide) + ex;
    var worth = wideOuter >= opts.budget * (opts.minWider || 1.15) &&
      tableHeight(opts, wide) <= tableHeight(opts, fitted) * (opts.maxTaller || 0.8);
    return worth ? wide : fitted;
  }

  // ── DOM side ────────────────────────────────────────────────────────────

  function cellGrid(table) {
    var rows = Array.prototype.slice.call(table.rows);
    var ncol = 0;
    for (var i = 0; i < rows.length; i++) {
      var cells = rows[i].cells;
      for (var j = 0; j < cells.length; j++) {
        if (cells[j].colSpan > 1 || cells[j].rowSpan > 1) return null;  // markdown never spans
      }
      ncol = Math.max(ncol, cells.length);
    }
    return { rows: rows, ncol: ncol };
  }

  function measurer(host, sample) {
    var m = document.createElement('div');
    var cs = getComputedStyle(sample);
    m.className = 'md-table-measure';
    m.style.cssText = 'position:absolute;left:0;top:0;visibility:hidden;pointer-events:none;' +
      'overflow-wrap:break-word;word-break:normal;white-space:normal;padding:0;border:0;';
    m.style.font = cs.font;
    m.style.lineHeight = cs.lineHeight;
    m.style.letterSpacing = cs.letterSpacing;
    host.appendChild(m);
    return m;
  }

  function fit(box) {
    var scroll = box.querySelector('.md-table-scroll');
    var table = scroll && scroll.querySelector('table');
    if (!table) return;
    var avail = scroll.clientWidth;
    if (!avail) return;                          // hidden; the observer retries
    var grid = cellGrid(table);
    if (!grid || !grid.ncol) return;
    var rows = grid.rows, nrow = rows.length, ncol = grid.ncol;

    // Back to the natural layout before measuring.
    var old = table.querySelector('colgroup.md-table-cols');
    if (old) old.remove();
    table.classList.remove('tf-fixed', 'tf-sticky');
    table.style.width = '';
    box.classList.remove('tf-overflow');

    var fontKey = getComputedStyle(table).fontSize + '|' + avail;
    box._tfKey = fontKey;
    if (nrow * ncol > MAX_CELLS) { markOverflow(box, scroll); return; }

    var t0 = Date.now();
    var sample = rows[0].cells[0];
    var m = measurer(box, sample);
    var html = [], minC = [], maxC = [], extra = [], em = parseFloat(getComputedStyle(table).fontSize) || 14;
    var r, c;
    try {
      for (c = 0; c < ncol; c++) { minC[c] = 0; maxC[c] = 0; extra[c] = 0; }
      for (r = 0; r < nrow; r++) {
        html[r] = [];
        for (c = 0; c < ncol; c++) {
          var cell = rows[r].cells[c];
          html[r][c] = cell ? cell.innerHTML : '';
          if (!cell) continue;
          var cs = getComputedStyle(cell);
          m.style.fontWeight = cs.fontWeight;
          m.innerHTML = html[r][c];
          m.style.width = 'min-content';
          minC[c] = Math.max(minC[c], Math.ceil(m.getBoundingClientRect().width));
          m.style.width = 'max-content';
          maxC[c] = Math.max(maxC[c], Math.ceil(m.getBoundingClientRect().width));
          var ex = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) +
                   parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth);
          extra[c] = Math.max(extra[c], Math.ceil(ex));
        }
      }
      var naturalWidth = sum(maxC) + sum(extra);
      if (naturalWidth <= avail) {
        // Everything fits on one line per cell: the browser's own layout is right.
        box.classList.remove('tf-cards-offered', 'tf-cards');
        return;
      }

      // A prose column may not grow past a readable measure.
      var cap = Math.max(16 * em, Math.min(avail * 0.92, 32 * em));
      var lo = [], hi = [];
      for (c = 0; c < ncol; c++) {
        lo[c] = Math.min(minC[c], cap);
        hi[c] = Math.max(lo[c], Math.min(maxC[c], cap));
      }
      var weights = [];
      for (r = 0; r < nrow; r++) {
        weights[r] = [];
        for (c = 0; c < ncol; c++) {
          var cl = rows[r].cells[c];
          weights[r][c] = cl ? getComputedStyle(cl).fontWeight : '400';
        }
      }
      var cache = {};
      var heightAt = function (row, col, width) {
        var key = row + ',' + col + ',' + width;
        if (cache[key] === undefined) {
          if (!html[row][col]) { cache[key] = 0; return 0; }
          m.style.fontWeight = weights[row][col];
          m.style.width = width + 'px';
          m.innerHTML = html[row][col];
          cache[key] = m.getBoundingClientRect().height;
        }
        return cache[key];
      };
      var widths = chooseLayout({
        nrow: nrow, ncol: ncol, min: lo, max: hi, extra: extra,
        // The budget leaves room for the 1px per column added below and the
        // collapsed table's outer border.
        heightAt: heightAt, budget: avail - ncol - 2, lambda: 1, whole: 12 * em,
        deadline: t0 + TIME_BUDGET_MS,
      });
    } finally {
      m.remove();
    }

    var cg = document.createElement('colgroup');
    cg.className = 'md-table-cols';
    var total = 0;
    for (c = 0; c < ncol; c++) {
      var col = document.createElement('col');
      var outer = widths[c] + extra[c] + 1;      // +1: sub-pixel text never clips
      col.style.width = outer + 'px';
      total += outer;
      cg.appendChild(col);
    }
    table.insertBefore(cg, table.firstChild);
    table.classList.add('tf-fixed');
    table.style.width = total + 'px';

    var overflow = total > avail + 1;
    if (overflow && widths[0] + extra[0] <= avail * 0.45) table.classList.add('tf-sticky');
    markOverflow(box, scroll);

    // Stacked cards for a prose table that would take several screens of
    // sideways scrolling on a phone.
    var chars = 0, n = 0;
    for (r = 1; r < nrow; r++) for (c = 0; c < ncol; c++) {
      var cc = rows[r].cells[c];
      if (cc) { chars += cc.textContent.trim().length; n++; }
    }
    var prose = n > 0 && chars / n >= 20;
    var offer = avail < NARROW_PX && ncol >= 3 && prose && total > avail * 1.3;
    offerCards(box, table, offer);
  }

  function markOverflow(box, scroll) {
    var more = scroll.scrollLeft + scroll.clientWidth < scroll.scrollWidth - 2;
    box.classList.toggle('tf-overflow', scroll.scrollWidth > scroll.clientWidth + 1);
    box.classList.toggle('tf-more-right', more);
  }

  function buildCards(table) {
    var rows = Array.prototype.slice.call(table.rows);
    var head = table.tHead && table.tHead.rows[0] ? table.tHead.rows[0] : null;
    var labels = head ? Array.prototype.map.call(head.cells, function (x) { return x.innerHTML; }) : [];
    var wrap = document.createElement('div');
    wrap.className = 'md-table-cards';
    rows.forEach(function (row) {
      if (head && row === head) return;
      var card = document.createElement('div');
      card.className = 'md-card';
      var title = document.createElement('div');
      title.className = 'md-card-title';
      title.innerHTML = row.cells[0] ? row.cells[0].innerHTML : '';
      card.appendChild(title);
      var dl = document.createElement('dl');
      dl.className = 'md-card-fields';
      for (var c = 1; c < row.cells.length; c++) {
        if (!row.cells[c].textContent.trim()) continue;
        var dt = document.createElement('dt');
        dt.innerHTML = labels[c] || '';
        var dd = document.createElement('dd');
        dd.innerHTML = row.cells[c].innerHTML;
        dl.appendChild(dt);
        dl.appendChild(dd);
      }
      card.appendChild(dl);
      wrap.appendChild(card);
    });
    return wrap;
  }

  function offerCards(box, table, offer) {
    box.classList.toggle('tf-cards-offered', offer);
    if (!offer) { box.classList.remove('tf-cards'); return; }
    if (!box.querySelector(':scope > .md-table-cards')) box.appendChild(buildCards(table));
    var tools = box.querySelector(':scope > .md-table-tools');
    if (!tools) {
      tools = document.createElement('div');
      tools.className = 'md-table-tools';
      tools.innerHTML = '<button type="button" data-mode="table">Table</button>' +
                        '<button type="button" data-mode="cards">Cards</button>';
      tools.addEventListener('click', function (e) {
        var b = e.target.closest('button[data-mode]');
        if (!b) return;
        e.stopPropagation();
        box._tfChosen = b.dataset.mode;
        setMode(box, b.dataset.mode);
      });
      box.insertBefore(tools, box.firstChild);
    }
    setMode(box, box._tfChosen || 'cards');
  }

  function setMode(box, mode) {
    box.classList.toggle('tf-cards', mode === 'cards');
    box.querySelectorAll(':scope > .md-table-tools button').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.mode === mode ? 'true' : 'false');
    });
  }

  /** Wrap one rendered <table> and keep its layout fitted to its container. */
  function attach(table) {
    if (table.closest('.md-table')) return table.closest('.md-table');
    var box = document.createElement('div');
    box.className = 'md-table';
    var scroll = document.createElement('div');
    scroll.className = 'md-table-scroll';
    var fade = document.createElement('div');
    fade.className = 'md-table-fade';
    table.parentNode.insertBefore(box, table);
    scroll.appendChild(table);
    box.appendChild(scroll);
    box.appendChild(fade);
    scroll.addEventListener('scroll', function () { markOverflow(box, scroll); }, { passive: true });
    var pending = false;
    var refit = function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        if (!box.isConnected) return;
        var key = getComputedStyle(table).fontSize + '|' + scroll.clientWidth;
        if (key !== box._tfKey) fit(box);
      });
    };
    if (typeof ResizeObserver === 'function') new ResizeObserver(refit).observe(box);
    refit();
    return box;
  }

  var api = { chooseWidths: chooseWidths, chooseLayout: chooseLayout, tableHeight: tableHeight, attach: attach, fit: fit };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TableFit = api;
})(typeof window !== 'undefined' ? window : this);
