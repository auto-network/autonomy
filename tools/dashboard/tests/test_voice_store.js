const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VOICE_STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-store.js');

function loadVoiceStore(opts) {
  const docListeners = {};
  const stores = Object.assign({}, (opts && opts.initialStores) || {});
  const storageData = Object.assign({}, (opts && opts.localStorage) || {});

  const document = {
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
  };

  const localStorage = {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(storageData, key) ? storageData[key] : null;
    },
    setItem(key, value) {
      storageData[key] = String(value);
    },
    removeItem(key) {
      delete storageData[key];
    },
  };

  const windowObj = {
    localStorage,
    Autonomy: {},
  };

  const Alpine = {
    store(name, obj) {
      if (obj !== undefined) {
        stores[name] = obj;
        return obj;
      }
      return stores[name];
    },
  };

  const sandbox = {
    window: windowObj,
    document,
    localStorage,
    Alpine,
    console,
    JSON,
    Object,
    Array,
    Date,
    Error,
    Number,
    String,
    Boolean,
    parseInt,
    parseFloat,
    setTimeout,
    clearTimeout,
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = Alpine;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VOICE_STORE_JS, 'utf8'), sandbox, { filename: 'voice-store.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  return {
    store: stores.voice,
    stores,
    storageData,
    window: windowObj,
  };
}

function toPlain(value) {
  return JSON.parse(JSON.stringify(value));
}

describe('voice store substrate', () => {
  it('registers Alpine.store("voice") with the pinned fields', () => {
    const h = loadVoiceStore();
    assert.ok(h.store);
    for (const field of [
      'boundSessionId',
      'micMode',
      'bufferText',
      'capsulePosition',
      'pendingRebindTarget',
      'discoverabilitySeen',
      'awayEventSessionId',
    ]) {
      assert.ok(Object.prototype.hasOwnProperty.call(h.store, field), field);
    }
    assert.equal(h.store.boundSessionId, '');
    assert.equal(h.store.micMode, 'idle');
  });

  it('hydrates discoverability and capsule position from localStorage', () => {
    const h = loadVoiceStore({
      localStorage: {
        'autonomy.voice.discoverabilitySeen': '1',
        'autonomy.voice.capsulePosition': JSON.stringify({ x: 120, y: 340 }),
      },
    });
    assert.equal(h.store.discoverabilitySeen, true);
    assert.deepEqual(toPlain(h.store.capsulePosition), { x: 120, y: 340 });
  });

  it('treats voice as disabled when the flags store is absent', () => {
    const h = loadVoiceStore();
    assert.equal(h.store.enabled, false);
    assert.deepEqual(
      toPlain(h.store.requestBind('session-a', { isLive: true })),
      { ok: false, reason: 'disabled' }
    );
  });

  it('binds a live session when voice.client_enabled is true', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: {
          get(name) {
            return name === 'voice.client_enabled';
          },
        },
      },
    });
    const result = h.store.requestBind('session-a', { isLive: true });
    assert.deepEqual(toPlain(result), { ok: true, reason: 'bound' });
    assert.equal(h.store.boundSessionId, 'session-a');
    assert.equal(h.store.micMode, 'listening');
  });

  it('rejects dead sessions even when voice is enabled', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    assert.deepEqual(
      toPlain(h.store.requestBind('session-a', { isLive: false })),
      { ok: false, reason: 'dead' }
    );
    assert.equal(h.store.boundSessionId, '');
  });

  it('requests confirmation before rebinding to a different session', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    const result = h.store.requestBind('session-b', { isLive: true });
    assert.deepEqual(toPlain(result), { ok: false, reason: 'confirm' });
    assert.equal(h.store.boundSessionId, 'session-a');
    assert.equal(h.store.pendingRebindTarget, 'session-b');
  });

  it('confirmRebind switches the bound session and clears pending state', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.requestBind('session-b', { isLive: true });
    assert.equal(h.store.confirmRebind(), true);
    assert.equal(h.store.boundSessionId, 'session-b');
    assert.equal(h.store.pendingRebindTarget, '');
    assert.equal(h.store.micMode, 'listening');
  });

  it('requestBind on the already-bound session is a no-op that preserves mute state', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.toggleMic();
    const result = h.store.requestBind('session-a', { isLive: true });
    assert.deepEqual(toPlain(result), { ok: true, reason: 'already_bound' });
    assert.equal(h.store.boundSessionId, 'session-a');
    assert.equal(h.store.micMode, 'muted');
    assert.equal(h.store.awayEventSessionId, '');
  });

  it('toggleMic cycles listening <-> muted and treats vad_paused as active listening', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    assert.equal(h.store.toggleMic(), true);
    assert.equal(h.store.micMode, 'muted');
    assert.equal(h.store.toggleMic(), true);
    assert.equal(h.store.micMode, 'listening');
    assert.equal(h.store.setMicMode('vad_paused'), true);
    assert.equal(h.store.toggleMic(), true);
    assert.equal(h.store.micMode, 'muted');
  });

  it('endSession clears session-scoped voice state but keeps persisted UI state', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.setCapsulePosition({ x: 90, y: 180 });
    h.store.markDiscoverabilitySeen();
    h.store.requestBind('session-a', { isLive: true });
    h.store.setBufferText('hello');
    h.store.pulseAwayEvent('session-a');
    h.store.endSession();
    assert.equal(h.store.boundSessionId, '');
    assert.equal(h.store.micMode, 'idle');
    assert.equal(h.store.bufferText, '');
    assert.equal(h.store.awayEventSessionId, '');
    assert.equal(h.store.discoverabilitySeen, true);
    assert.deepEqual(toPlain(h.store.capsulePosition), { x: 90, y: 180 });
  });

  it('persists capsule position and discoverability state', () => {
    const h = loadVoiceStore();
    assert.equal(h.store.setCapsulePosition({ x: 200, y: 320 }), true);
    h.store.markDiscoverabilitySeen();
    assert.equal(h.storageData['autonomy.voice.discoverabilitySeen'], '1');
    assert.equal(h.storageData['autonomy.voice.capsulePosition'], JSON.stringify({ x: 200, y: 320 }));
  });

  it('clears pending rebind when the target or bound session ends', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.requestBind('session-b', { isLive: true });
    h.store.clearPendingRebindIfSessionEnded('session-b');
    assert.equal(h.store.pendingRebindTarget, '');

    h.store.requestBind('session-b', { isLive: true });
    h.store.confirmRebind();
    h.store.clearPendingRebindIfSessionEnded('session-b');
    assert.equal(h.store.boundSessionId, '');
    assert.equal(h.store.micMode, 'idle');
  });
});
