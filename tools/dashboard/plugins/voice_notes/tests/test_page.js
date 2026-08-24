const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../../../..');
const PAGE_JS = path.join(REPO_ROOT, 'tools/dashboard/plugins/voice_notes/page.js');

function loadPage() {
  const voice = {
    enabled: true,
    boundSessionId: 'session-a',
    micMode: 'muted',
    connState: 'ok',
    setMicMode(mode) { this.micMode = mode; return true; },
  };
  const windowObj = {
    Autonomy: {
      voice: {
        claimSurface() { return { release() {} }; },
        subscribe() { return function() {}; },
        clearBuffer() { return true; },
      },
      topbar: { set() { return { destroy() {} }; } },
    },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    crypto: { randomUUID() { return 'test-uuid'; } },
    setTimeout,
  };
  const Alpine = {
    store(name) {
      if (name === 'voice') return voice;
      if (name === 'sessions') return { 'session-a': { isLive: true } };
      return null;
    },
  };
  const sandbox = {
    window: windowObj,
    Alpine,
    console,
    Date,
    Intl,
    JSON,
    Math,
    Number,
    Object,
    String,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(PAGE_JS, 'utf8'), sandbox, { filename: 'page.js' });
  const page = sandbox.voiceNotesPage();
  page.$nextTick = function(callback) {
    if (typeof callback === 'function') callback();
    return Promise.resolve();
  };
  page.$refs = {
    noteEditor: {
      selectionStart: 0,
      setSelectionRange() {},
      scrollTop: 0,
      scrollHeight: 0,
      focus() {},
    },
  };
  return { page, voice };
}

describe('Voice Notes live transcript projection', () => {
  it('replaces the active dictated range when Whisper deletes and rewrites words', () => {
    const { page } = loadPage();
    page.draft = {
      note_id: 'note-test',
      title: '',
      body: 'Before  after',
      created_at: '',
      updated_at: '',
    };
    page.talking = true;
    page.dictationAnchor = 7;
    page.dictatedText = '';

    page._onVoiceSnapshot({ revision: 1, text: 'the first hypothesis' });
    assert.equal(page.draft.body, 'Before the first hypothesis after');

    page._onVoiceSnapshot({ revision: 2, text: 'the corrected phrase' });
    assert.equal(page.draft.body, 'Before the corrected phrase after');

    page._onVoiceSnapshot({ revision: 1, text: 'stale words' });
    assert.equal(page.draft.body, 'Before the corrected phrase after');
  });

  it('keeps dictated text in the note when the user pauses and the platform clears', () => {
    const { page, voice } = loadPage();
    page.draft = {
      note_id: 'note-test', title: '', body: '', created_at: '', updated_at: '',
    };
    page.talking = true;
    page.dictationAnchor = 0;
    page._onVoiceSnapshot({ revision: 1, text: 'Keep this thought.' });

    page._stopTalking();
    page._onVoiceSnapshot({ revision: 2, text: '', update: 'clear' });

    assert.equal(page.draft.body, 'Keep this thought.');
    assert.equal(page.talking, false);
    assert.equal(voice.micMode, 'muted');
  });

  it('preserves word boundaries when dictation starts inside existing prose', async () => {
    const { page } = loadPage();
    page.draft = {
      note_id: 'note-test', title: '', body: 'Beforeafter', created_at: '', updated_at: '',
    };
    page.$refs.noteEditor.selectionStart = 6;

    assert.equal(await page._startTalking(), true);
    page._onVoiceSnapshot({ revision: 100, text: 'new words' });

    assert.equal(page.draft.body, 'Before new words after');
  });
});
