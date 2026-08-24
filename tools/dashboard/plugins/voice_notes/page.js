// Voice Notes — a proof-of-concept plugin for route-scoped live dictation.
//
// The platform keeps microphone/WebSocket/Whisper ownership. This surface
// leases only the visible controls and consumes revisioned whole-buffer
// snapshots, replacing the current dictated range when a partial is rewritten.

const VOICE_NOTES_DRAFT_KEY = 'autonomy.plugin.voice-notes.draft';

function voiceNotesPage() {
  return {
    notes: [],
    draft: null,
    loading: true,
    saving: false,
    savedPulse: false,
    dirty: false,
    talking: false,
    voiceError: '',
    persistenceError: '',
    voiceLease: null,
    unsubscribeVoice: null,
    topbarHandle: null,
    dictationAnchor: 0,
    dictatedText: '',
    lastVoiceRevision: -1,

    async init() {
      this._setTopbar();
      this.voiceLease = window.Autonomy.voice.claimSurface({
        caption: 'plugin',
        controls: 'plugin',
      });
      if (!this.voiceLease) {
        this.voiceError = 'This build does not grant Voice Notes live transcription.';
      }
      this.unsubscribeVoice = window.Autonomy.voice.subscribe(
        snapshot => this._onVoiceSnapshot(snapshot),
      );

      // Entering a voice-owned writing surface is a safe pause boundary. Never
      // leave a hidden session composer actively listening behind this page.
      const voice = this.voice;
      if (voice && (voice.micMode === 'listening' || voice.micMode === 'vad_paused')) {
        voice.setMicMode('muted');
      }
      this._restoreDraft();
      await this._loadNotes();
    },

    destroy() {
      if (this.talking) this._stopTalking();
      if (typeof this.unsubscribeVoice === 'function') this.unsubscribeVoice();
      if (this.voiceLease && typeof this.voiceLease.release === 'function') {
        this.voiceLease.release();
      }
      if (this.topbarHandle && typeof this.topbarHandle.destroy === 'function') {
        this.topbarHandle.destroy();
      }
      this.unsubscribeVoice = null;
      this.voiceLease = null;
      this.topbarHandle = null;
    },

    get voice() {
      try { return Alpine.store('voice') || null; } catch (_) { return null; }
    },

    get hasDraft() {
      return !!(this.draft && this.draft.note_id);
    },

    get isTalking() {
      const voice = this.voice;
      return !!(
        this.talking && voice &&
        (voice.micMode === 'listening' || voice.micMode === 'vad_paused')
      );
    },

    get micBusy() {
      const voice = this.voice;
      return !!(voice && voice.connState === 'reconnecting');
    },

    get voiceStatus() {
      if (this.voiceError) return this.voiceError;
      if (this.micBusy) return 'Reconnecting to transcription…';
      if (this.isTalking) return 'Listening — tap the microphone to pause';
      if (!this.hasDraft) return 'Create a note to begin';
      return 'Paused — edit freely, then tap to continue';
    },

    get wordCount() {
      const body = (this.draft && this.draft.body) || '';
      return body.trim() ? body.trim().split(/\s+/).length : 0;
    },

    get saveLabel() {
      if (this.saving) return 'Saving…';
      if (this.savedPulse) return 'Saved';
      return 'Save note';
    },

    async newNote() {
      if (this.talking) this._stopTalking();
      this._clearVoiceBuffer();
      const now = new Date().toISOString();
      this.draft = {
        note_id: this._newId(),
        title: '',
        body: '',
        created_at: now,
        updated_at: now,
      };
      this.dirty = false;
      this.persistenceError = '';
      this._persistDraft();
      await this.$nextTick();
      const editor = this.$refs && this.$refs.noteEditor;
      if (editor) editor.focus();
    },

    async selectNote(note) {
      if (!note) return;
      if (this.talking) this._stopTalking();
      this._clearVoiceBuffer();
      this.draft = { ...note };
      this.dirty = false;
      this.persistenceError = '';
      this._persistDraft();
      await this.$nextTick();
      const editor = this.$refs && this.$refs.noteEditor;
      if (editor) editor.focus();
    },

    onDraftInput() {
      if (!this.draft) return;
      this.dirty = true;
      this.savedPulse = false;
      this._persistDraft();
    },

    async saveNote() {
      if (!this.draft || this.saving) return false;
      if (this.talking) this._stopTalking();
      this.saving = true;
      this.persistenceError = '';
      const title = String(this.draft.title || '').trim() || this._titleFromBody();
      this.draft.title = title || 'Untitled note';
      try {
        const response = await window.Autonomy.fetch(
          '/api/plugins/voice-notes/notes/' + encodeURIComponent(this.draft.note_id),
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: this.draft.title, body: this.draft.body || '' }),
          },
        );
        const data = await response.json();
        if (!response.ok || !data || data.ok !== true) {
          throw new Error((data && data.error) || 'Save failed');
        }
        this.draft = { ...data.note };
        const index = this.notes.findIndex(note => note.note_id === this.draft.note_id);
        if (index >= 0) this.notes.splice(index, 1, { ...this.draft });
        else this.notes.unshift({ ...this.draft });
        this.notes.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
        this.dirty = false;
        this.savedPulse = true;
        this._persistDraft();
        window.setTimeout(() => { this.savedPulse = false; }, 1400);
        return true;
      } catch (error) {
        this.persistenceError = error && error.message ? error.message : String(error);
        return false;
      } finally {
        this.saving = false;
      }
    },

    async toggleTalking() {
      if (!this.draft || this.micBusy) return false;
      if (this.talking) {
        this._stopTalking();
        return true;
      }
      return this._startTalking();
    },

    async _startTalking() {
      this.voiceError = '';
      const voice = this.voice;
      if (!voice || voice.enabled !== true) {
        this.voiceError = 'Voice capture is disabled in dashboard settings.';
        return false;
      }
      if (!this._ensureVoiceBinding()) return false;

      const editor = this.$refs && this.$refs.noteEditor;
      let anchor = editor && Number.isFinite(editor.selectionStart)
        ? editor.selectionStart
        : String(this.draft.body || '').length;
      let body = String(this.draft.body || '');
      anchor = Math.max(0, Math.min(anchor, body.length));
      if (anchor > 0 && !/\s/.test(body.charAt(anchor - 1))) {
        body = body.slice(0, anchor) + ' ' + body.slice(anchor);
        anchor += 1;
      }
      // Preserve a separator after the live replacement range too. Without
      // this, dictating into the middle of a sentence would join the final
      // hypothesis directly to the word following the caret.
      if (anchor < body.length && !/\s/.test(body.charAt(anchor))) {
        body = body.slice(0, anchor) + ' ' + body.slice(anchor);
      }
      this.draft.body = body;
      this.dictationAnchor = anchor;
      this.dictatedText = '';
      this.lastVoiceRevision = -1;
      this.talking = true;
      this._clearVoiceBuffer();
      voice.setMicMode('listening');
      this.dirty = true;
      this._persistDraft();
      return true;
    },

    _stopTalking() {
      const voice = this.voice;
      if (voice && voice.boundSessionId) voice.setMicMode('muted');
      this.talking = false;
      this.dictatedText = '';
      this.lastVoiceRevision = -1;
      this._clearVoiceBuffer();
      this._persistDraft();
    },

    _onVoiceSnapshot(snapshot) {
      if (!this.talking || !this.draft || !snapshot) return;
      const revision = Number(snapshot.revision);
      if (Number.isFinite(revision) && revision <= this.lastVoiceRevision) return;
      this.lastVoiceRevision = Number.isFinite(revision) ? revision : this.lastVoiceRevision + 1;

      const nextText = String(snapshot.text || '');
      const body = String(this.draft.body || '');
      const start = Math.max(0, Math.min(this.dictationAnchor, body.length));
      const end = Math.max(start, Math.min(start + this.dictatedText.length, body.length));
      this.draft.body = body.slice(0, start) + nextText + body.slice(end);
      this.dictatedText = nextText;
      this.dirty = true;
      this._persistDraft();

      this.$nextTick(() => {
        const editor = this.$refs && this.$refs.noteEditor;
        if (!editor) return;
        const caret = this.dictationAnchor + this.dictatedText.length;
        editor.setSelectionRange(caret, caret);
        editor.scrollTop = editor.scrollHeight;
      });
    },

    _ensureVoiceBinding() {
      const voice = this.voice;
      if (!voice) return false;
      if (voice.boundSessionId) return true;
      let sessions = {};
      try { sessions = Alpine.store('sessions') || {}; } catch (_) {}
      const candidates = Object.entries(sessions)
        .filter(([, session]) => session && session.isLive !== false)
        .sort(([, left], [, right]) => this._sessionTime(right) - this._sessionTime(left));
      if (!candidates.length) {
        this.voiceError = 'Voice Notes needs one live session for the transcription pipe.';
        return false;
      }
      const result = voice.requestBind(candidates[0][0], { isLive: true });
      if (!result || result.ok !== true) {
        this.voiceError = 'Could not attach the microphone to a live transcription session.';
        return false;
      }
      // requestBind begins in listening mode; the caller immediately establishes
      // the note range and then explicitly resumes, keeping one control path.
      voice.setMicMode('muted');
      return true;
    },

    _clearVoiceBuffer() {
      if (window.Autonomy.voice && typeof window.Autonomy.voice.clearBuffer === 'function') {
        window.Autonomy.voice.clearBuffer('clear');
      }
    },

    async _loadNotes() {
      this.loading = true;
      try {
        const response = await window.Autonomy.fetch('/api/plugins/voice-notes/notes');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not load notes');
        this.notes = Array.isArray(data.notes) ? data.notes : [];
      } catch (error) {
        this.persistenceError = error && error.message ? error.message : String(error);
      } finally {
        this.loading = false;
      }
    },

    _restoreDraft() {
      try {
        const raw = window.localStorage.getItem(VOICE_NOTES_DRAFT_KEY);
        const value = raw ? JSON.parse(raw) : null;
        if (value && value.note_id && typeof value.body === 'string') {
          this.draft = value;
          this.dirty = true;
        }
      } catch (_) {}
    },

    _persistDraft() {
      try {
        if (this.draft) {
          window.localStorage.setItem(VOICE_NOTES_DRAFT_KEY, JSON.stringify(this.draft));
        } else {
          window.localStorage.removeItem(VOICE_NOTES_DRAFT_KEY);
        }
      } catch (_) {}
    },

    _newId() {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return 'note-' + window.crypto.randomUUID();
      }
      return 'note-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
    },

    _titleFromBody() {
      const words = String((this.draft && this.draft.body) || '').trim().split(/\s+/).filter(Boolean);
      if (!words.length) return '';
      const title = words.slice(0, 7).join(' ');
      return words.length > 7 ? title + '…' : title;
    },

    _sessionTime(session) {
      if (!session) return 0;
      const raw = session.lastActivity || session.last_activity || session.startedAt || 0;
      const numeric = Number(raw);
      if (Number.isFinite(numeric)) return numeric;
      const parsed = Date.parse(raw);
      return Number.isFinite(parsed) ? parsed : 0;
    },

    noteTitle(note) {
      return String((note && note.title) || '').trim() || 'Untitled note';
    },

    notePreview(note) {
      const text = String((note && note.body) || '').trim().replace(/\s+/g, ' ');
      return text || 'Empty note';
    },

    noteDate(note) {
      const raw = note && (note.updated_at || note.created_at);
      const date = raw ? new Date(raw) : null;
      if (!date || Number.isNaN(date.getTime())) return '';
      return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(date);
    },

    _setTopbar() {
      if (!window.Autonomy || !window.Autonomy.topbar ||
          typeof window.Autonomy.topbar.set !== 'function') return;
      this.topbarHandle = window.Autonomy.topbar.set({
        title: 'Voice Notes',
        subtitle: 'A notebook that listens',
      });
    },
  };
}
