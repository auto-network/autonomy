const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VOICE_UI_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-ui.js');

function loadVoiceUi(initialVoiceStore) {
  const stores = {
    voice: Object.assign({
      enabled: false,
      boundSessionId: '',
      micMode: 'idle',
      discoverabilitySeen: false,
      awayEventSessionId: '',
      requestBind(sessionId) {
        this.boundSessionId = sessionId;
        this.micMode = 'listening';
        return { ok: true, reason: 'bound' };
      },
      toggleMic() {
        this.micMode = this.micMode === 'muted' ? 'listening' : 'muted';
        return true;
      },
      endSession() {
        this.boundSessionId = '';
        this.micMode = 'idle';
      },
      markDiscoverabilitySeen() {
        this.discoverabilitySeen = true;
      },
    }, initialVoiceStore || {}),
  };

  const sandbox = {
    window: { Autonomy: {}, setTimeout, clearTimeout },
    Alpine: {
      store(name) {
        return stores[name];
      },
    },
    console,
  };
  sandbox.window.Alpine = sandbox.Alpine;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VOICE_UI_JS, 'utf8'), sandbox, { filename: 'voice-ui.js' });
  return {
    ui: sandbox.window.Autonomy.voice.ui,
    voice: stores.voice,
  };
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

describe('voice dot UI helpers', () => {
  it('treats flag-off live sessions as non-interactive idle dots', () => {
    const h = loadVoiceUi({ enabled: false });
    assert.equal(h.ui.sessionDotState('session-a', { isLive: true }), 'flag_off');
    assert.equal(h.ui.dotDisabled('session-a', { isLive: true }), true);
    assert.equal(h.ui.showMicGlyph('session-a', { isLive: true }), false);
  });

  it('marks enabled live sessions as bindable idle dots', () => {
    const h = loadVoiceUi({ enabled: true, discoverabilitySeen: false });
    assert.equal(h.ui.sessionDotState('session-a', { isLive: true }), 'idle');
    assert.equal(h.ui.dotDisabled('session-a', { isLive: true }), false);
    assert.deepEqual(plain(h.ui.viewerDotClasses('session-a', { isLive: true, isWorking: true })), {
      'indicator--green': true,
      'indicator--gray': false,
      'indicator-dot--working': true,
      'voice-dot--bindable': true,
      'voice-dot--hint': true,
      'voice-dot--active': false,
      'voice-dot--muted': false,
      'voice-dot--away': false,
    });
  });

  it('renders the bound session as active and muted when micMode is muted', () => {
    const h = loadVoiceUi({
      enabled: true,
      boundSessionId: 'session-a',
      micMode: 'muted',
      awayEventSessionId: 'session-a',
    });
    assert.equal(h.ui.sessionDotState('session-a', { isLive: true }), 'muted');
    assert.equal(h.ui.showMicGlyph('session-a', { isLive: true }), true);
    assert.equal(h.ui.showMuteSlash('session-a', { isLive: true }), true);
    assert.equal(h.ui.cardDotClasses('session-a', { isLive: true, isWorking: false })['voice-dot--away'], true);
  });

  it('binds on idle click and marks discoverability seen', () => {
    const h = loadVoiceUi({ enabled: true, discoverabilitySeen: false });
    const event = { currentTarget: {} };
    assert.equal(h.ui.onClick(event, 'session-a', { isLive: true }), true);
    assert.equal(h.voice.boundSessionId, 'session-a');
    assert.equal(h.voice.micMode, 'listening');
    assert.equal(h.voice.discoverabilitySeen, true);
  });

  it('toggles mute on active click', () => {
    const h = loadVoiceUi({ enabled: true, boundSessionId: 'session-a', micMode: 'listening' });
    const event = { currentTarget: {} };
    assert.equal(h.ui.onClick(event, 'session-a', { isLive: true }), true);
    assert.equal(h.voice.micMode, 'muted');
  });

  it('uses tmux_session as the canonical cross-surface bind key for session rows', () => {
    const h = loadVoiceUi({ enabled: true });
    const cardRow = {
      session_id: 'u-123',
      tmux_session: 'auto-0515-163913',
      is_live: true,
    };
    const cardKey = h.ui.voiceBindKey(cardRow);
    assert.equal(cardKey, 'auto-0515-163913');
    h.voice.requestBind(cardKey, { isLive: true });
    assert.equal(h.ui.sessionDotState('auto-0515-163913', { isLive: true }), 'listening');
  });

  it('ends the active session on long press and suppresses the click follow-up', async () => {
    const h = loadVoiceUi({ enabled: true, boundSessionId: 'session-a', micMode: 'listening' });
    const target = {};
    h.ui.onPointerDown({ currentTarget: target }, 'session-a', { isLive: true });
    await new Promise((resolve) => setTimeout(resolve, 650));
    assert.equal(h.voice.boundSessionId, '');
    assert.equal(h.voice.micMode, 'idle');
    assert.equal(h.ui.onClick({ currentTarget: target }, 'session-a', { isLive: true }), false);
  });
});
