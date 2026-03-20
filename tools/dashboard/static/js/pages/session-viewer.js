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
          const res = await fetch('/api/upload', { method: 'POST', body: form });
          const data = await res.json();
          if (data.ok) {
            this.uploadPath = data.path;
            // Append path to message input
            const sep = this.inputText.trim() ? '\n' : '';
            this.inputText = this.inputText + sep + data.path;
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
          const res = await fetch('/api/session/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: this.inputText.trim(), tmux_session: this._tmuxSession }),
          });
          const data = await res.json();
          if (data.ok) {
            this.inputText = '';
            this.clearUpload();
            // Dismiss mobile keyboard
            if (this.$refs.messageInput) this.$refs.messageInput.blur();
          } else {
            console.warn('[sessionViewer] send error:', data.error);
          }
        } catch (e) {
          console.warn('[sessionViewer] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      async _poll() {
        try {
          const res = await fetch(`${this._tailUrl}?after=${this.offset}`);
          const data = await res.json();

          this.isLive = data.is_live;
          if (data.offset !== undefined) this.offset = data.offset;

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
