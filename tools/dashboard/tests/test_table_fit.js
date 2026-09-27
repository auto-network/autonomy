/**
 * Column sizing for markdown tables (static/js/lib/table-fit.js, chooseWidths).
 *
 * Cells are modelled as words of fixed-width characters wrapped at spaces, so
 * each case is exact. The cases are the operator's 2026-09-27 phone
 * screenshots:
 *   - a two-column table that already fits must fill the width and wrap nothing;
 *   - a short numeric column ("2,856 px") beside prose must get its full width
 *     first, not a character or two;
 *   - a prose table wider than the screen may exceed the budget only while
 *     width still buys height, and never below any column's longest word.
 *
 * Run: node --test tools/dashboard/tests/test_table_fit.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');

const { chooseWidths, chooseLayout, tableHeight } = require(path.join(__dirname, '..', 'static', 'js', 'lib', 'table-fit.js'));

const CH = 8;       // px per character
const LINE = 20;    // px per line

function lines(text, width) {
  const words = text.split(/\s+/).filter(Boolean);
  if (!words.length) return 0;
  let n = 1, used = 0;
  for (const word of words) {
    const w = word.length * CH;
    const need = used ? used + CH + w : w;
    if (used && need > width) { n++; used = w; } else { used = need; }
  }
  return n;
}

function model(rows) {
  const ncol = rows[0].length;
  const min = [], max = [];
  for (let c = 0; c < ncol; c++) {
    min[c] = Math.max(...rows.map(r => Math.max(...r[c].split(/\s+/).map(w => w.length * CH))));
    max[c] = Math.max(...rows.map(r => r[c].length * CH));
  }
  return {
    nrow: rows.length, ncol, min, max, extra: new Array(ncol).fill(16),
    heightAt: (r, c, w) => lines(rows[r][c], w) * LINE,
    height(widths) { return rows.reduce((s, row, r) => s + Math.max(...row.map((_, c) => this.heightAt(r, c, widths[c]))), 0); },
  };
}

const outer = (m, w) => w.reduce((s, x, c) => s + x + m.extra[c], 0);

describe('chooseWidths', () => {
  it('a table that fits fills the budget and wraps nothing', () => {
    const m = model([
      ['Step', 'Result'],
      ['Install', '161 s'],
      ['"Go to your workspace" to session page', '2 s'],
      ['Host terminal answering after launch', '19 s'],
    ]);
    const w = chooseWidths({ ...m, budget: 400, lambda: 1 });
    assert.equal(m.height(w), 4 * LINE, 'one line per row');
    assert.ok(outer(m, w) <= 400);
  });

  it('a short numeric column gets its full width before prose widens', () => {
    const m = model([
      ['Layout', 'Height', 'Readability'],
      ['Today', '2,856 px', 'one or two words per line'],
      ['Greedy column sizing, swipe sideways', '1,751 px', 'four or five words per line, but still blank space beside short cells'],
      ['Stacked cards', '3,666 px', 'each row a card: Item as its title, the other columns as labelled lines at full width'],
    ]);
    const w = chooseWidths({ ...m, budget: 340, lambda: 1, whole: 12 * CH });
    assert.ok(w[1] >= m.max[1], `Height column ${w[1]}px must hold "2,856 px" (${m.max[1]}px) on one line`);
    for (let c = 0; c < m.ncol; c++) assert.ok(w[c] >= m.min[c], `column ${c} never below its longest word`);
  });

  it('past the budget it widens only while width buys height (lambda)', () => {
    const prose = 'unbuilt; the bootstrap page probes CLIs in the dashboard container, which has none on a Compose node';
    const rows = [['Item', 'Owner', 'State', 'Done when']];
    for (let i = 0; i < 8; i++) rows.push([`item ${i}: a feature with a medium title`, 'REL (proposed)', prose, 'each harness signs in from onboarding with a throwaway account']);
    const m = model(rows);
    const tight = chooseWidths({ ...m, budget: 260, lambda: 1e9 });
    const loose = chooseWidths({ ...m, budget: 260, lambda: 1 });
    const minOuter = outer(m, m.min);
    assert.ok(outer(m, tight) <= Math.max(260, minOuter) + 4 * 4, 'an infinite lambda stays at the budget (or the minimum)');
    assert.ok(outer(m, loose) > outer(m, tight), 'lambda 1 spends width past the budget');
    assert.ok(m.height(loose) < m.height(tight), 'and buys height with it');
    const narrowest = chooseWidths({ ...m, budget: 0, lambda: 1e9 });
    assert.deepEqual(narrowest, m.min, 'no budget, no widening');
    const whole = chooseWidths({ ...m, budget: 0, lambda: 1e9, whole: 15 * CH });
    assert.equal(whole[1], m.max[1], '"REL (proposed)" is short: kept whole even with no budget');
  });

  it('widening beats the browser: fewer lines than proportional sharing at the same width', () => {
    const m = model([
      ['Layout', 'Width', 'Height', 'Readability'],
      ['Today', '465 px, right side cut off', '2,856 px', 'one or two words per line'],
      ['Greedy column sizing, swipe sideways', '634 px, 2.5 screens', '1,751 px', 'four or five words per line, but still blank space beside short cells'],
      ['Stacked cards', '257 px, no swiping', '3,666 px', 'each row a card: Item as its title, the other columns as labelled lines at full width'],
    ]);
    const budget = 340;
    // What a browser does when the table fits between min and max: share the
    // spare width in proportion to each column's (max - min).
    const spare = budget - outer(m, m.min);
    const span = m.max.map((x, c) => x - m.min[c]);
    const total = span.reduce((a, b) => a + b, 0);
    const prop = m.min.map((x, c) => x + Math.floor(spare * span[c] / total));
    // Pure height minimisation beats proportional sharing.
    const pure = chooseWidths({ ...m, budget, lambda: 1e9 });
    assert.ok(m.height(pure) < m.height(prop), `greedy ${m.height(pure)}px vs proportional ${m.height(prop)}px`);
    // Keeping short cells whole may cost a line of height (measured: 420 vs 400
    // here) and in exchange never splits "2,856 px"; proportional sharing does.
    const kept = chooseWidths({ ...m, budget, lambda: 1e9, whole: 12 * CH });
    assert.ok(kept[2] >= m.max[2] && prop[2] < m.max[2]);
    assert.ok(m.height(kept) <= m.height(prop) + LINE);
  });

  it('stops at the deadline', () => {
    const m = model([['a b c d e f g h', 'i j k l m n o p']]);
    let t = 0;
    const w = chooseWidths({ ...m, budget: 1000, lambda: 1, deadline: 5, now: () => (t += 10) });
    assert.ok(Array.isArray(w) && w.length === 2);
  });
});

describe('chooseLayout', () => {
  const opts = (m, budget) => ({ ...m, budget, lambda: 1, whole: 12 * CH });

  it('stays inside the screen when overflowing would save little (a few pixels for one line)', () => {
    const m = model([
      ['Layout', 'Width', 'Height', 'Readability'],
      ['Today', '465 px, right side cut off', '2,856 px', 'one or two words per line'],
      ['Greedy column sizing, swipe sideways', '634 px, 2.5 screens', '1,751 px', 'four or five words per line, but still blank space beside short cells'],
      ['Stacked cards', '257 px, no swiping', '3,666 px', 'each row a card: Item as its title, the other columns as labelled lines at full width'],
    ]);
    // At 385 px the widening would overflow by 7 px to save one line (320 vs 300 px).
    const w = chooseLayout(opts(m, 385));
    assert.ok(outer(m, w) <= 385, `fitted: ${outer(m, w)}px in 385px`);
    // At 340 px the same widening is 1.15 times wider and 29 percent shorter: worth a scroll.
    assert.ok(outer(m, chooseLayout(opts(m, 340))) > 340);
  });

  it('scrolls sideways when that is clearly shorter', () => {
    const prose = 'unbuilt; the bootstrap page probes CLIs in the dashboard container, which has none on a Compose node (code read 2026-09-27)';
    const rows = [['Item', 'Owner', 'State', 'Done when']];
    for (let i = 0; i < 10; i++) rows.push([`item ${i}: the harness sign-in walkthrough for Claude, Codex and Grok`, 'REL (proposed)', prose, 'each harness signs in from onboarding with a throwaway account; needs Q1 answered']);
    const m = model(rows);
    const w = chooseLayout(opts(m, 260));
    const fitted = chooseWidths({ ...opts(m, 260), lambda: Infinity });
    assert.ok(outer(m, w) >= 260 * 1.15);
    assert.ok(tableHeight(m, w) <= tableHeight(m, fitted) * 0.8);
  });
});
