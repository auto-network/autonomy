(function () {

  function _formatProject(project) {
    return project.replace(/-home-jeremy-?/, '').replace(/workspace-/, '') || 'home';
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('sessionViewerPage', () => ({
      // State machine
      state: 'loading',   // 'loading' | 'ready' | 'error'
      errorMsg: '',

      // Session identity (read from URL on init)
      project: '',
      sessionId: '',
      projectLabel: '',

      // Entries and polling
      entries: [],
      offset: 0,
      isLive: false,
      entryCount: 0,
      autoScroll: true,
      _pollTimer: null,

      // Tool ID tracking (for matching tool_result to tool_use)
      _toolMap: {},

      // Input
      inputText: '',
      sending: false,

      // Upload
      uploading: false,
      uploadPreview: '',
      uploadFilename: '',
      uploadPath: '',

      // Session type (host or container)
      sessionType: '',

      // Link terminal state
      linkState: 'idle',   // 'idle' | 'picking' | 'handshaking' | 'confirmed' | 'failed'
      linkCandidates: [],
      selectedTmux: '',
      linkError: '',
      HANDSHAKE_STRING: '[dashboard] confirming terminal link \u2014 please reply with I SEE IT',

      // API paths set from URL params
      _tailUrl: '',

      formatTime(ts) {
        if (!ts) return '';
        try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
      },

      toolLabel(toolId) {
        const info = this._toolMap[toolId];
        return info ? info.tool_name + ' result' : 'result';
      },

      onScroll() {
        const el = this.$refs.entriesContainer;
        if (!el) return;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
        this.autoScroll = atBottom;
      },

      resumeScroll() {
        this.autoScroll = true;
        const el = this.$refs.entriesContainer;
        if (el) el.scrollTop = el.scrollHeight;
      },

      async onFileSelected(event) {
        const file = event.target.files && event.target.files[0];
        if (!file) return;
        this.uploadFilename = file.name;

        // Show preview for images
        if (file.type.startsWith('image/')) {
          this.uploadPreview = URL.createObjectURL(file);
        } else {
          this.uploadPreview = '';
        }

        this.uploading = true;
        try {
          const form = new FormData();
          form.append('file', file);
          if (this._tmuxSession) form.append('tmux_session', this._tmuxSession);
          const res = await fetch('/api/upload', { method: 'POST', body: form });
          const data = await res.json();
          if (data.ok) {
            this.uploadPath = data.path;
          } else {
            console.warn('[sessionViewer] upload error:', data.error);
          }
        } catch (e) {
          console.warn('[sessionViewer] upload failed:', e);
        } finally {
          this.uploading = false;
          // Reset file input so the same file can be re-selected
          if (this.$refs.fileInput) this.$refs.fileInput.value = '';
        }
      },

      clearUpload() {
        this.uploadPreview = '';
        this.uploadFilename = '';
        this.uploadPath = '';
      },

      async sendMessage() {
        const text = this.inputText.trim();
        if ((!text && !this.uploadPath) || this.sending) return;
        this.sending = true;
        try {
          const _send = async (msg) => {
            const res = await fetch('/api/session/send', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message: msg, tmux_session: this._tmuxSession }),
            });
            return res.json();
          };

          if (this.uploadPath) {
            // Send image path first so Claude Code injects it as an image content block
            const pathData = await _send(this.uploadPath);
            if (!pathData.ok) {
              console.warn('[sessionViewer] send error (path):', pathData.error);
              return;
            }
            // Send follow-up text only if there is any
            if (text) {
              await new Promise(r => setTimeout(r, 200));
              const textData = await _send(text);
              if (!textData.ok) {
                console.warn('[sessionViewer] send error (text):', textData.error);
                return;
              }
            }
          } else {
            const data = await _send(text);
            if (!data.ok) {
              console.warn('[sessionViewer] send error:', data.error);
              return;
            }
          }

          this.inputText = '';
          this.clearUpload();
          // Dismiss mobile keyboard
          if (this.$refs.messageInput) this.$refs.messageInput.blur();
        } catch (e) {
          console.warn('[sessionViewer] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      async showLinkPicker() {
        try {
          const res = await fetch('/api/terminal/unclaimed');
          this.linkCandidates = await res.json();
        } catch (e) {
          this.linkCandidates = [];
        }
        this.selectedTmux = '';
        this.linkError = '';
        this.linkState = 'picking';
      },

      async confirmLink() {
        if (!this.selectedTmux) return;
        this.linkState = 'handshaking';
        try {
          await fetch('/api/session/send-handshake', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tmux_session: this.selectedTmux }),
          });
          // Poll JSONL for handshake string to appear (up to 10s)
          const deadline = Date.now() + 10000;
          while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 1500));
            await this._poll();
            const found = this.entries.slice(-5).some(
              e => e.type === 'user' && (e.content || '').includes(this.HANDSHAKE_STRING)
            );
            if (found) {
              await fetch('/api/session/confirm-link', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  project: this.project,
                  session_id: this.sessionId,
                  tmux_session: this.selectedTmux,
                }),
              });
              this._tmuxSession = this.selectedTmux;
              this.linkState = 'confirmed';
              return;
            }
          }
          this.linkState = 'failed';
          this.linkError = 'Handshake timed out \u2014 handshake message did not appear in session log';
        } catch (e) {
          this.linkState = 'failed';
          this.linkError = 'Error during handshake: ' + (e.message || e);
        }
      },

      resetLink() {
        this.linkState = 'idle';
        this.selectedTmux = '';
        this.linkError = '';
        this.linkCandidates = [];
      },

      async _poll() {
        try {
          const res = await fetch(`${this._tailUrl}?after=${this.offset}`);
          const data = await res.json();

          this.isLive = data.is_live;
          if (data.offset !== undefined) this.offset = data.offset;

          // Set session type from first response
          if (data.type && !this.sessionType) this.sessionType = data.type;

          // Auto-detect tmux session from per-file meta (handles page reload after linking)
          if (data.tmux_session && !this._tmuxSession) this._tmuxSession = data.tmux_session;

          if (data.entries && data.entries.length > 0) {
            // Track tool_use IDs for result matching
            for (const entry of data.entries) {
              if (entry.type === 'tool_use' && entry.tool_id) {
                this._toolMap[entry.tool_id] = {
                  tool_name: entry.tool_name || '?',
                  tool_headline: entry.tool_headline || '',
                };
              }
            }
            this.entries = [...this.entries, ...data.entries];
            this.entryCount = this.entries.length;

            // Auto-scroll
            if (this.autoScroll) {
              this.$nextTick(() => {
                const el = this.$refs.entriesContainer;
                if (el) el.scrollTop = el.scrollHeight;
              });
            }
          }

          if (this.state === 'loading') this.state = 'ready';

          // Stop polling if session is complete and no new entries
          if (!data.is_live && this.offset > 0 && (!data.entries || data.entries.length === 0)) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
          }
        } catch (e) {
          if (this.state === 'loading') {
            this.errorMsg = 'Failed to connect to session';
            this.state = 'error';
          }
        }
      },

      async init() {
        // Parse URL: /session/{project}/{session_id}
        const m = window.location.pathname.match(/^\/session\/([^/]+)\/(.+)$/);
        if (!m) {
          this.errorMsg = 'Invalid session URL';
          this.state = 'error';
          return;
        }
        this.project = decodeURIComponent(m[1]);
        this.sessionId = m[2];
        this.projectLabel = _formatProject(this.project);

        this._tailUrl = `/api/session/${encodeURIComponent(this.project)}/${encodeURIComponent(this.sessionId)}/tail`;

        // tmux session name: from query param only (no auto-detect — avoids misfiring to wrong session)
        const params = new URLSearchParams(window.location.search);
        this._tmuxSession = params.get('tmux') || '';

        // First poll (immediate)
        await this._poll();

        // If no entries came back and state is still loading, set ready (empty session)
        if (this.state === 'loading') this.state = 'ready';

        // Start polling interval
        this._pollTimer = setInterval(() => this._poll(), 1500);
      },

      destroy() {
        if (this._pollTimer) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
      },
    }));
  });
})();
