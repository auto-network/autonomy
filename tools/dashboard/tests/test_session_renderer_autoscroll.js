const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const RENDERER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-renderer.js');

function renderer() {
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(RENDERER_JS, 'utf8'), sandbox, {
    filename: 'session-renderer.js',
  });
  return sandbox.window.SessionRenderer;
}

function context() {
  const scroller = { scrollHeight: 1000, scrollTop: 800, clientHeight: 200 };
  return {
    scroller,
    ctx: {
      autoScroll: true,
      _lastScrollTop: 800,
      $refs: { entriesContainer: scroller },
    },
  };
}

describe('session renderer sticky-bottom latch', () => {
  it('stays latched when new content grows below the current bottom', () => {
    const r = renderer();
    const { ctx, scroller } = context();

    scroller.scrollHeight = 1100; // new tile laid out; scrollTop is old bottom
    r.onScroll.call(ctx);

    assert.equal(ctx.autoScroll, true);
    assert.equal(ctx._lastScrollTop, 800);
  });

  it('unlatches on upward movement and relatches only at the bottom', () => {
    const r = renderer();
    const { ctx, scroller } = context();

    scroller.scrollTop = 650;
    r.onScroll.call(ctx);
    assert.equal(ctx.autoScroll, false, 'deliberate upward scroll disengages');

    scroller.scrollHeight = 1200;
    r.onScroll.call(ctx);
    assert.equal(ctx.autoScroll, false, 'more content does not relatch off-bottom');

    scroller.scrollTop = 995; // 5px from the 1000px maximum
    r.onScroll.call(ctx);
    assert.equal(ctx.autoScroll, true, 'reaching bottom relatches');
  });

  it('resumeScroll records the programmatic bottom as the new baseline', () => {
    const r = renderer();
    const { ctx, scroller } = context();
    ctx.autoScroll = false;
    ctx._lastScrollTop = 300;

    r.resumeScroll.call(ctx);

    assert.equal(ctx.autoScroll, true);
    assert.equal(scroller.scrollTop, 1000);
    assert.equal(ctx._lastScrollTop, 1000);
  });
});
