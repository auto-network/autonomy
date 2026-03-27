const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const { deriveToolbarState, toolbarElements } = require('../static/js/lib/toolbar-state.js');

// ── deriveToolbarState ─────────────────────────────────────────────

describe('deriveToolbarState', () => {
  it('returns DISCONNECTED when both false', () => {
    assert.equal(deriveToolbarState(false, false), 'DISCONNECTED');
  });

  it('returns PICKER when chatOpen=true, chatConnected=false', () => {
    assert.equal(deriveToolbarState(true, false), 'PICKER');
  });

  it('returns LIVE_UI when chatOpen=false, chatConnected=true', () => {
    assert.equal(deriveToolbarState(false, true), 'LIVE_UI');
  });

  it('returns LIVE_CHAT when both true', () => {
    assert.equal(deriveToolbarState(true, true), 'LIVE_CHAT');
  });
});

// ── toolbarElements per state ──────────────────────────────────────

describe('toolbarElements — DISCONNECTED', () => {
  const el = toolbarElements('DISCONNECTED');

  it('state is DISCONNECTED', () => assert.equal(el.state, 'DISCONNECTED'));
  it('showIterNav is true', () => assert.equal(el.showIterNav, true));
  it('captureVisible is false', () => assert.equal(el.captureVisible, false));
  it('row2Session is false', () => assert.equal(el.row2Session, false));
  it('titleText is "experiment"', () => assert.equal(el.titleText, 'experiment'));
  it('primeVisible is false', () => assert.equal(el.primeVisible, false));
  it('disconnectVisible is false', () => assert.equal(el.disconnectVisible, false));
  it('chatIconClass is chat-disconnected', () => assert.equal(el.chatIconClass, 'chat-disconnected'));
});

describe('toolbarElements — PICKER', () => {
  const el = toolbarElements('PICKER');

  it('state is PICKER', () => assert.equal(el.state, 'PICKER'));
  it('showIterNav is false', () => assert.equal(el.showIterNav, false));
  it('captureVisible is false', () => assert.equal(el.captureVisible, false));
  it('row2Session is false', () => assert.equal(el.row2Session, false));
  it('titleText is null', () => assert.equal(el.titleText, null));
  it('primeVisible is false', () => assert.equal(el.primeVisible, false));
  it('disconnectVisible is false', () => assert.equal(el.disconnectVisible, false));
  it('chatIconClass is chat-open', () => assert.equal(el.chatIconClass, 'chat-open'));
});

describe('toolbarElements — LIVE_UI', () => {
  const el = toolbarElements('LIVE_UI');

  it('state is LIVE_UI', () => assert.equal(el.state, 'LIVE_UI'));
  it('showIterNav is true', () => assert.equal(el.showIterNav, true));
  it('captureVisible is true', () => assert.equal(el.captureVisible, true));
  it('row2Session is false', () => assert.equal(el.row2Session, false));
  it('titleText is "experiment"', () => assert.equal(el.titleText, 'experiment'));
  it('primeVisible is false', () => assert.equal(el.primeVisible, false));
  it('disconnectVisible is false', () => assert.equal(el.disconnectVisible, false));
  it('chatIconClass is chat-connected-hidden', () => assert.equal(el.chatIconClass, 'chat-connected-hidden'));
});

describe('toolbarElements — LIVE_CHAT', () => {
  const el = toolbarElements('LIVE_CHAT');

  it('state is LIVE_CHAT', () => assert.equal(el.state, 'LIVE_CHAT'));
  it('showIterNav is false', () => assert.equal(el.showIterNav, false));
  it('captureVisible is false', () => assert.equal(el.captureVisible, false));
  it('row2Session is true', () => assert.equal(el.row2Session, true));
  it('titleText is "experiment"', () => assert.equal(el.titleText, 'experiment'));
  it('primeVisible is true', () => assert.equal(el.primeVisible, true));
  it('disconnectVisible is true', () => assert.equal(el.disconnectVisible, true));
  it('chatIconClass is chat-connected-shown', () => assert.equal(el.chatIconClass, 'chat-connected-shown'));
});
