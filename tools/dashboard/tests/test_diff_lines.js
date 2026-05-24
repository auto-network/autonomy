// Validates diff-lines.js counts only changed lines, not total lines.
//
// Run with: node tools/dashboard/tests/test_diff_lines.js
// Exits 0 on success, 1 on any failure.

global.window = {};
require('../static/js/lib/diff-lines.js');

const { lineDiffCounts } = window.AutonomyDiffLines;

const CASES = [
  // [label, old, new, expected added, expected removed, expected estimate?]
  ['identical',                 'a\nb\nc',   'a\nb\nc',   0, 0, false],
  ['middle line changed',       'a\nb\nc',   'a\nB\nc',   1, 1, false],
  ['appended line',             'a\nb\nc',   'a\nb\nc\nd', 1, 0, false],
  ['removed middle lines',      'a\nb\nc\nd', 'a\nd',     0, 2, false],
  ['empty new_string',          'a\nb',      '',          0, 2, false],
  ['empty old_string',          '',          'a\nb',      2, 0, false],
  ['both empty',                '',          '',          0, 0, false],
  ['no newline either side',    'foo',       'bar',       1, 1, false],
  ['10 ctx + 1 change',         arr(10, (i) => 'line' + i).join('\n'),
                                 arr(10, (i) => i === 5 ? 'CHANGED' : 'line' + i).join('\n'),
                                 1, 1, false],
  // 'a\n' splits to ['a',''], 'a\nb' splits to ['a','b'] — LCS sees the
  // trailing empty as deleted and 'b' as added. Defensible (git would
  // call this +1 only via "\ No newline at end of file", but the renderer
  // tile's accuracy budget is fine with +1/-1 for this edge).
  ['trailing-newline transition','a\n',      'a\nb',      1, 1, false],
];

function arr(n, fn) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(fn(i));
  return out;
}

let pass = 0, fail = 0;
for (const [label, o, n, eAdd, eRem, eEst] of CASES) {
  const r = lineDiffCounts(o, n);
  const ok = r.added === eAdd && r.removed === eRem && Boolean(r.estimate) === eEst;
  const status = ok ? 'PASS' : 'FAIL';
  console.log(`${status}  ${label.padEnd(30)} +${r.added} -${r.removed} estimate=${r.estimate}`);
  if (!ok) {
    console.log(`      expected: +${eAdd} -${eRem} estimate=${eEst}`);
    fail++;
  } else {
    pass++;
  }
}

// Cap fallback: > MAX_LINES on either side returns estimate: true with cheap counts.
const huge_old = arr(6000, (i) => 'a' + i).join('\n');
const huge_new = arr(6000, (i) => 'b' + i).join('\n');
const huge_r = lineDiffCounts(huge_old, huge_new);
const huge_ok = huge_r.estimate === true && huge_r.added === 6000 && huge_r.removed === 6000;
console.log(`${huge_ok ? 'PASS' : 'FAIL'}  ${'cap fallback (>5000 lines)'.padEnd(30)} +${huge_r.added} -${huge_r.removed} estimate=${huge_r.estimate}`);
huge_ok ? pass++ : fail++;

console.log('---');
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
