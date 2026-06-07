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
  const fetchCalls = [];

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
  const fetchImpl = (opts && opts.fetchImpl) || (async function() {
    throw new Error('unexpected fetch');
  });

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
    fetch: async function(url, init) {
      fetchCalls.push({ url, init: init || null });
      return fetchImpl(url, init);
    },
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
    FormData,
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = Alpine;
  sandbox.window.fetch = sandbox.fetch;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VOICE_STORE_JS, 'utf8'), sandbox, { filename: 'voice-store.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  return {
    store: stores.voice,
    stores,
    storageData,
    window: windowObj,
    fetchCalls,
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
      'sheetOpen',
      'sheetMode',
      'sheetError',
      'sheetResumeListeningOnDismiss',
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

  it('openSheet keeps active listening live (no implicit mute) so the transcript streams into the sheet', () => {
    // Pinned behavior (operator directive): opening the sheet must NOT mute —
    // the full transcript streams live into the visible editor. Overrides the
    // old spec L127 "opening the sheet implicitly mutes capture".
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    assert.equal(h.store.openSheet(), true);
    assert.equal(h.store.sheetOpen, true);
    assert.equal(h.store.sheetMode, 'partial');
    assert.equal(h.store.sheetResumeListeningOnDismiss, false);
    assert.equal(h.store.micMode, 'listening');
  });

  it('openSheet preserves explicit muted state and dismissSheet does not unmute it', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.toggleMic();
    assert.equal(h.store.micMode, 'muted');
    assert.equal(h.store.openSheet(), true);
    assert.equal(h.store.sheetResumeListeningOnDismiss, false);
    assert.equal(h.store.dismissSheet(), true);
    assert.equal(h.store.micMode, 'muted');
  });

  it('openSheet keeps vad_paused capture live (no implicit mute) and leaves it unchanged', () => {
    // Same pinned behavior: vad_paused is a listening sub-state; opening the
    // sheet no longer mutes it, and dismiss leaves it as-is (no resume needed).
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.setMicMode('vad_paused');
    assert.equal(h.store.openSheet(), true);
    assert.equal(h.store.micMode, 'vad_paused');
    assert.equal(h.store.sheetResumeListeningOnDismiss, false);
    assert.equal(h.store.dismissSheet(), true);
    assert.equal(h.store.micMode, 'vad_paused');
  });

  it('openSheet stays closed when voice is disabled or no session is bound', () => {
    const disabled = loadVoiceStore({
      initialStores: {
        flags: { get() { return false; } },
      },
    });
    assert.equal(disabled.store.openSheet(), false);
    assert.equal(disabled.store.sheetOpen, false);

    const unbound = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    assert.equal(unbound.store.openSheet(), false);
    assert.equal(unbound.store.sheetOpen, false);
  });

  it('expandSheet and collapseSheet move the sheet mode without affecting buffer', () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.setBufferText('dictated text');
    h.store.openSheet();
    assert.equal(h.store.expandSheet(), true);
    assert.equal(h.store.sheetMode, 'full');
    assert.equal(h.store.collapseSheet(), true);
    assert.equal(h.store.sheetMode, 'partial');
    assert.equal(h.store.bufferText, 'dictated text');
  });

  it('clearBuffer empties the shared buffer and clears sheet errors', () => {
    const h = loadVoiceStore();
    h.store.setBufferText('draft');
    h.store.sheetError = 'Send failed';
    h.store.clearBuffer();
    assert.equal(h.store.bufferText, '');
    assert.equal(h.store.sheetError, '');
  });

  it('sendBuffer stages through the durable outbox engine and restores listening on success', async () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    const staged = [];
    h.window.newOutboxId = function () { return 'ob_voice_store'; };
    h.window.stageOutboxSend = function (sessionId, outbox, options) {
      staged.push({ sessionId, outbox: toPlain(outbox), options: toPlain(options) });
      return Promise.resolve(true);
    };
    h.store.requestBind('session-a', { isLive: true });
    h.store.setBufferText('  ship this  ');
    h.store.openSheet();
    assert.equal(await h.store.sendBuffer(), true);
    assert.deepEqual(staged, [{
      sessionId: 'session-a',
      outbox: {
        localId: 'ob_voice_store',
        state: 'sending',
        source: 'voice',
        text: 'ship this',
        ts: staged[0].outbox.ts,
      },
      options: { tmuxSession: 'session-a' },
    }]);
    assert.equal(h.fetchCalls.length, 0, 'voice-store must not POST directly');
    assert.equal(h.store.bufferText, '');
    assert.equal(h.store.sheetOpen, false);
    assert.equal(h.store.sheetMode, 'partial');
    assert.equal(h.store.sheetError, '');
    assert.equal(h.store.micMode, 'listening');
    assert.equal(h.store.sheetResumeListeningOnDismiss, false);
  });

  it('sendBuffer uses the same durable outbox path even when no viewer is mounted', async () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    const staged = [];
    h.window.newOutboxId = function () { return 'ob_cross_session'; };
    h.window.stageOutboxSend = function (sessionId, outbox, options) {
      staged.push({ sessionId, outbox: toPlain(outbox), options: toPlain(options) });
      return Promise.resolve(true);
    };

    h.store.requestBind('session-a', { isLive: true });
    h.store.setBufferText('send without mounted viewer');
    h.store.openSheet();

    assert.equal(await h.store.sendBuffer(), true);
    assert.deepEqual(staged, [{
      sessionId: 'session-a',
      outbox: {
        localId: 'ob_cross_session',
        state: 'sending',
        source: 'voice',
        text: 'send without mounted viewer',
        ts: staged[0].outbox.ts,
      },
      options: { tmuxSession: 'session-a' },
    }]);
    assert.equal(h.fetchCalls.length, 0, 'voice-store must not direct POST when durable owner exists');
    assert.equal(h.store.bufferText, '');
    assert.equal(h.store.sheetOpen, false);
    assert.equal(h.store.sheetMode, 'partial');
  });

  it('sendBuffer leaves the sheet open and preserves the buffer when the outbox engine is unavailable', async () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.store.requestBind('session-a', { isLive: true });
    h.store.setBufferText('retry me');
    h.store.openSheet();
    assert.equal(await h.store.sendBuffer(), false);
    assert.equal(h.store.sheetOpen, true);
    assert.equal(h.store.bufferText, 'retry me');
    assert.equal(h.store.sheetError, 'Send failed. Message outbox is unavailable.');
    // openSheet no longer mutes (live-capture directive), so the session stays
    // listening through the sheet and a failed send.
    assert.equal(h.store.micMode, 'listening');
  });

  it('sendBuffer leaves the sheet open and preserves the buffer when durable staging throws', async () => {
    const h = loadVoiceStore({
      initialStores: {
        flags: { get() { return true; } },
      },
    });
    h.window.stageOutboxSend = function () {
      throw new Error('stage failed');
    };
    h.store.requestBind('session-a', { isLive: true });
    h.store.setBufferText('retry me');
    h.store.openSheet();
    assert.equal(await h.store.sendBuffer(), false);
    assert.equal(h.store.sheetOpen, true);
    assert.equal(h.store.bufferText, 'retry me');
    assert.equal(h.store.sheetError, 'Send failed. Session connection dropped. Retry after reconnecting or end voice on this session.');
  });

  it('sendBuffer reports a session-ended error when no session is bound', async () => {
    const h = loadVoiceStore();
    h.store.setBufferText('retry me');
    assert.equal(await h.store.sendBuffer(), false);
    assert.equal(h.store.sheetError, 'Send failed. Session is no longer available.');
  });
});

describe('voice store attachments', () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));

  function boundStore(fetchImpl) {
    const h = loadVoiceStore({
      initialStores: { flags: { get() { return true; } } },
      fetchImpl,
    });
    h.store.requestBind('session-a', { isLive: true });
    return h;
  }

  it('uploads a picked file to /api/upload with the bound session and stores the returned path', async () => {
    const h = boundStore(async function (url) {
      if (url === '/api/upload') {
        return { ok: true, async json() {
          return { ok: true, files: [{ path: '/tmp/shot.png', rel_path: '.uploads/shot.png', mime: 'image/png', size: 42 }] };
        } };
      }
      throw new Error('unexpected url ' + url);
    });
    h.store.addAttachmentFiles([{ name: 'shot.png', type: 'text/plain' }]);
    // Pending immediately: chip staged, no path yet -> blocks send.
    assert.equal(h.store.attachments.length, 1);
    assert.equal(h.store.attachmentsPending, true);
    assert.equal(h.store.canSend, false);
    await flush();
    const up = h.fetchCalls.find((c) => c.url === '/api/upload');
    assert.ok(up, 'posted to /api/upload');
    assert.equal(up.init.method, 'POST');
    assert.equal(up.init.body.get('tmux_session'), 'session-a');
    assert.equal(h.store.attachments[0].path, '/tmp/shot.png');
    assert.equal(h.store.attachments[0].rel_path, '.uploads/shot.png');
    assert.equal(h.store.attachmentsPending, false);
    assert.equal(h.store.canSend, true);   // landed attachment, no text needed
  });

  it('drops the chip and surfaces an error when the upload fails', async () => {
    const h = boundStore(async function () {
      return { ok: true, async json() { return { ok: false, error: 'nope' }; } };
    });
    h.store.addAttachmentFiles([{ name: 'bad.png', type: 'text/plain' }]);
    await flush();
    assert.equal(h.store.attachments.length, 0);
    assert.equal(h.store.sheetError, 'Attachment upload failed.');
  });

  it('_buildSendBody composes paths above text, matching the keyboard composer', () => {
    const h = boundStore();
    h.store.attachments = [{ id: 1, path: '/tmp/a.png' }, { id: 2, path: '/tmp/b.png' }];
    h.store.setBufferText('look at these');
    assert.equal(h.store._buildSendBody(), '/tmp/a.png\n/tmp/b.png\n\nlook at these');
    h.store.setBufferText('');
    assert.equal(h.store._buildSendBody(), '/tmp/a.png\n/tmp/b.png');   // attachment-only
    h.store.attachments = [];
    h.store.setBufferText('text only');
    assert.equal(h.store._buildSendBody(), 'text only');
  });

  it('sendBuffer blocks while an attachment is still uploading', async () => {
    const h = boundStore(async function () { throw new Error('must not send'); });
    h.store.attachments = [{ id: 1, path: null, name: 'wip.png' }];   // pending
    h.store.setBufferText('with a file');
    assert.equal(await h.store.sendBuffer(), false);
    assert.equal(h.store.sheetError, 'Attachment still uploading…');
    assert.equal(h.fetchCalls.length, 0);
  });

  it('sendBuffer stages the composed body and clears attachments on success', async () => {
    const h = boundStore();
    const staged = [];
    h.window.newOutboxId = function () { return 'ob_attachment'; };
    h.window.stageOutboxSend = function (sessionId, outbox, options) {
      staged.push({ sessionId, outbox: toPlain(outbox), options: toPlain(options) });
      return Promise.resolve(true);
    };
    h.store.attachments = [{ id: 1, path: '/tmp/pic.png', name: 'pic.png' }];
    h.store.setBufferText('ship it');
    h.store.openSheet();
    assert.equal(await h.store.sendBuffer(), true);
    assert.equal(staged[0].outbox.text, '/tmp/pic.png\n\nship it');
    assert.equal(staged[0].sessionId, 'session-a');
    assert.equal(staged[0].options.tmuxSession, 'session-a');
    assert.equal(h.fetchCalls.length, 0, 'voice-store must not POST attachments directly');
    assert.equal(h.store.attachments.length, 0);   // cleared after send
  });

  it('removeAttachment and clearAttachments drop staged files', () => {
    const h = boundStore();
    h.store.attachments = [{ id: 1, path: '/tmp/a' }, { id: 2, path: '/tmp/b' }];
    h.store.removeAttachment(1);
    assert.equal(h.store.attachments.length, 1);
    assert.equal(h.store.attachments[0].id, 2);
    h.store.clearAttachments();
    assert.equal(h.store.attachments.length, 0);
  });
});
