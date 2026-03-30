/**
 * Node.js unit tests for screenshot.js — URL construction, defensive status updates,
 * and capture state machine transitions.
 * Run: node --test tools/dashboard/tests/test_screenshot.js
 */
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');

// Provide a minimal window/document stub for the module to attach to
global.window = {};
global.document = { getElementById: () => null };

const Screenshot = require('../static/js/lib/screenshot.js');

// ── Suite 1: _screenshotUrl ──────────────────────────────────────────

describe('_screenshotUrl', () => {
  it('basic URL', () => {
    assert.equal(
      Screenshot._screenshotUrl('abc123'),
      '/api/design/abc123/screenshot'
    );
  });

  it('with session name', () => {
    assert.equal(
      Screenshot._screenshotUrl('abc123', 'my-session'),
      '/api/design/abc123/screenshot?tmux_session=my-session'
    );
  });

  it('encodes special characters in session name', () => {
    const url = Screenshot._screenshotUrl('abc', 'has spaces&stuff');
    assert(url.includes('has%20spaces%26stuff'), 'should encode spaces and ampersands');
  });

  it('no session parameter when sessionName is empty string', () => {
    assert.equal(
      Screenshot._screenshotUrl('x', ''),
      '/api/design/x/screenshot'
    );
  });

  it('no session parameter when sessionName is null', () => {
    assert.equal(
      Screenshot._screenshotUrl('x', null),
      '/api/design/x/screenshot'
    );
  });

  it('no session parameter when sessionName is undefined', () => {
    assert.equal(
      Screenshot._screenshotUrl('x', undefined),
      '/api/design/x/screenshot'
    );
  });
});

// ── Suite 2: _updateScreenshotStatus ─────────────────────────────────

describe('_updateScreenshotStatus', () => {
  beforeEach(() => {
    // Reset window state before each test
    global.window = {};
    global.document = { getElementById: () => null };
  });

  it('calls setScreenshotStatus when designPage exists', () => {
    let called = null;
    global.window._designPage = { setScreenshotStatus: (m) => { called = m; } };
    Screenshot._updateScreenshotStatus('x', 'test msg');
    assert.equal(called, 'test msg');
  });

  it('does not throw when designPage has no setScreenshotStatus', () => {
    global.window._designPage = {};
    assert.doesNotThrow(() => Screenshot._updateScreenshotStatus('x', 'test'));
  });

  it('does not throw when designPage is null', () => {
    global.window._designPage = null;
    assert.doesNotThrow(() => Screenshot._updateScreenshotStatus('x', 'test'));
  });

  it('does not throw when designPage is undefined', () => {
    delete global.window._designPage;
    assert.doesNotThrow(() => Screenshot._updateScreenshotStatus('x', 'test'));
  });

  it('falls back to DOM element when designPage missing', () => {
    let textSet = null;
    global.document.getElementById = (id) => {
      if (id === 'design-screenshot-status') return { set textContent(v) { textSet = v; } };
      return null;
    };
    Screenshot._updateScreenshotStatus('x', 'fallback msg');
    assert.equal(textSet, 'fallback msg');
  });

  it('does not throw when DOM element also missing', () => {
    global.document.getElementById = () => null;
    assert.doesNotThrow(() => Screenshot._updateScreenshotStatus('x', 'no element'));
  });
});

// ── Suite 3: _handleScreenshotResponse ───────────────────────────────

describe('_handleScreenshotResponse', () => {
  beforeEach(() => {
    global.window = {};
    global.document = { getElementById: () => null };
  });

  it('sets "Screenshot saved" status for non-injected response', () => {
    let statusMsg = null;
    global.window._designPage = { setScreenshotStatus: (m) => { statusMsg = m; } };
    Screenshot._handleScreenshotResponse('x', { injected: false });
    assert(statusMsg.startsWith('Screenshot saved'), `expected "Screenshot saved..." got "${statusMsg}"`);
  });

  it('sets "Screenshot injected" status for injected response', () => {
    let statusMsg = null;
    global.window._designPage = { setScreenshotStatus: (m) => { statusMsg = m; } };
    Screenshot._handleScreenshotResponse('x', { injected: true });
    assert(statusMsg.startsWith('Screenshot injected'), `expected "Screenshot injected..." got "${statusMsg}"`);
  });

  it('does not throw when designPage is missing', () => {
    assert.doesNotThrow(() => Screenshot._handleScreenshotResponse('x', { injected: true }));
  });
});

// ── Suite 4: Capture state machine (experiment component logic) ──────

describe('capture state machine', () => {
  /**
   * Minimal simulation of the design.js captureScreenshot state machine.
   * Tests the state transitions without requiring Alpine or a browser.
   */
  function makeStateMachine(manualCaptureFn) {
    return {
      captureState: 'idle',
      expId: 'test-1',
      _tmuxSession: '',
      captureScreenshot: async function () {
        if (this.captureState === 'working') return;
        this.captureState = 'working';
        var self = this;
        try {
          await manualCaptureFn(this.expId, this._tmuxSession || '');
          self.captureState = 'success';
        } catch (e) {
          self.captureState = 'error';
        }
        // In real code this is setTimeout(..., 3000); we skip the delay for tests
        self.captureState = 'idle';
      },
    };
  }

  it('idle -> working on captureScreenshot()', async () => {
    let stateWhenCalled = null;
    const sm = makeStateMachine(async function (expId) {
      stateWhenCalled = sm.captureState;
    });
    assert.equal(sm.captureState, 'idle');
    await sm.captureScreenshot();
    assert.equal(stateWhenCalled, 'working');
  });

  it('working -> success on successful capture', async () => {
    let resolveCapture;
    const capturePromise = new Promise(r => { resolveCapture = r; });
    const sm = {
      captureState: 'idle',
      expId: 'test-1',
      _tmuxSession: '',
      _statesObserved: [],
      captureScreenshot: async function () {
        if (this.captureState === 'working') return;
        this.captureState = 'working';
        this._statesObserved.push(this.captureState);
        var self = this;
        try {
          await capturePromise;
          self.captureState = 'success';
          self._statesObserved.push(self.captureState);
        } catch (e) {
          self.captureState = 'error';
          self._statesObserved.push(self.captureState);
        }
      },
    };

    const p = sm.captureScreenshot();
    assert.equal(sm.captureState, 'working');
    resolveCapture(); // success
    await p;
    assert.equal(sm.captureState, 'success');
    assert.deepStrictEqual(sm._statesObserved, ['working', 'success']);
  });

  it('working -> error on failed capture', async () => {
    let rejectCapture;
    const capturePromise = new Promise((_, rej) => { rejectCapture = rej; });
    const sm = {
      captureState: 'idle',
      expId: 'test-1',
      _tmuxSession: '',
      captureScreenshot: async function () {
        if (this.captureState === 'working') return;
        this.captureState = 'working';
        var self = this;
        try {
          await capturePromise;
          self.captureState = 'success';
        } catch (e) {
          self.captureState = 'error';
        }
      },
    };

    const p = sm.captureScreenshot();
    assert.equal(sm.captureState, 'working');
    rejectCapture(new Error('fail'));
    await p;
    assert.equal(sm.captureState, 'error');
  });

  it('success -> idle after timeout (simulated)', async () => {
    const sm = makeStateMachine(async () => {}); // succeeds immediately
    await sm.captureScreenshot();
    // makeStateMachine immediately resets to idle (simulating the 3s timeout)
    assert.equal(sm.captureState, 'idle');
  });

  it('error -> idle after timeout (simulated)', async () => {
    const sm = makeStateMachine(async () => { throw new Error('boom'); });
    await sm.captureScreenshot();
    // makeStateMachine immediately resets to idle (simulating the 3s timeout)
    assert.equal(sm.captureState, 'idle');
  });

  it('working blocks double-click (returns early)', async () => {
    let callCount = 0;
    let resolveCapture;
    const capturePromise = new Promise(r => { resolveCapture = r; });

    const sm = {
      captureState: 'idle',
      expId: 'test-1',
      _tmuxSession: '',
      captureScreenshot: async function () {
        if (this.captureState === 'working') return;
        this.captureState = 'working';
        callCount++;
        var self = this;
        try {
          await capturePromise;
          self.captureState = 'success';
        } catch (e) {
          self.captureState = 'error';
        }
      },
    };

    // First click — starts capture
    const p1 = sm.captureScreenshot();
    assert.equal(callCount, 1);

    // Second click while working — should return early
    const p2 = sm.captureScreenshot();
    assert.equal(callCount, 1, 'second click should not increment callCount');

    resolveCapture();
    await p1;
    await p2;
  });
});
