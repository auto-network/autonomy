/**
 * The usage strip renders a Codex row that has only a weekly window.
 *
 * OpenAI suspended the rolling 5-hour Codex window on 2026-07-12, so a
 * `dashboard.harness.usage` row carrying only `windows.long` is CORRECT,
 * not damaged (bead auto-pojkz). This locks in that the strip presents the
 * absent 5h slot as "no reading" — a dimmed bar and a `--` countdown — and
 * never as a fault: no STALE badge and no hot/warn tone.
 *
 * We pull just the strip-rendering functions out of app.js and run them in
 * a Node `vm` sandbox, the same slice technique test_fatal_modal.js uses.
 *
 * Run: node --test tools/dashboard/tests/test_harness_strip_window.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const APP_JS = path.join(REPO_ROOT, 'tools/dashboard/static/app.js');

// Pull the named functions out of app.js so the test does not have to boot
// the whole dashboard shell. Each is self-contained; if one is renamed or
// deleted this throws loudly rather than silently testing nothing.
function sliceFunctions(source, names) {
  return names.map((name) => {
    const start = source.indexOf(`function ${name}(`);
    if (start === -1) throw new Error(`app.js no longer defines ${name}()`);
    let depth = 0;
    let seenBody = false;
    for (let i = start; i < source.length; i += 1) {
      const ch = source[i];
      if (ch === '{') { depth += 1; seenBody = true; }
      else if (ch === '}') {
        depth -= 1;
        if (seenBody && depth === 0) return source.slice(start, i + 1);
      }
    }
    throw new Error(`could not find the end of ${name}()`);
  }).join('\n\n');
}

const sandbox = { module: {}, exports: {} };
vm.createContext(sandbox);
vm.runInContext(
  sliceFunctions(fs.readFileSync(APP_JS, 'utf8'), [
    '_esc',
    'formatRateWindowLabel',
    'formatHarnessUsageCountdown',
    'harnessWindowTone',
    'harnessWindowStale',
    'renderHarnessStripWindow',
  ]),
  sandbox,
);

// A live Codex reading under the post-2026-07-12 regime: weekly window only.
const CODEX_WINDOWS = {
  long: {
    used_percent: 1.0,
    window_minutes: 10080,
    resets_at: Math.floor(Date.now() / 1000) + 6 * 24 * 3600,
  },
};

describe('renderHarnessStripWindow — Codex row with only a weekly window', () => {
  const short = sandbox.renderHarnessStripWindow(CODEX_WINDOWS.short, '5h');
  const long = sandbox.renderHarnessStripWindow(CODEX_WINDOWS.long, '7d');

  it('labels the absent 5h slot from the fallback', () => {
    assert.match(short, /harness-strip-window">5h</);
  });

  it('shows no reading rather than a wrong one', () => {
    assert.match(short, /width:0%/);
    assert.match(short, /harness-strip-reset">--</);
  });

  it('marks the absent slot unavailable, which renders dimmed', () => {
    assert.match(short, /class="[^"]*is-unavailable/);
  });

  it('applies no hot or warn tone to the absent slot', () => {
    assert.doesNotMatch(short, /is-hot|is-warn/);
  });

  it('renders the weekly window normally beside it', () => {
    assert.match(long, /harness-strip-window">7d</);
    assert.doesNotMatch(long, /is-unavailable/);
    assert.match(long, /harness-strip-reset">[0-9]/);
  });
});

describe('harnessWindowStale — an absent window is not stale', () => {
  it('does not badge a missing window', () => {
    assert.equal(sandbox.harnessWindowStale(CODEX_WINDOWS.short), false);
  });

  it('does not badge a window whose reset is still ahead', () => {
    assert.equal(sandbox.harnessWindowStale(CODEX_WINDOWS.long), false);
  });

  it('still badges a window whose reset has elapsed', () => {
    assert.equal(
      sandbox.harnessWindowStale({ resets_at: Math.floor(Date.now() / 1000) - 60 }),
      true,
    );
  });
});
