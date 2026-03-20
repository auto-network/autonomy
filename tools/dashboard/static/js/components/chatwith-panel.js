// Chat With panel — reusable Alpine component for rich session messaging.
//
// Replaces xterm.js terminal with the session viewer's rich HTML message feed,
// multimodal input bar (auto-expanding textarea, file attachment, two-send
// image injection), and JSONL polling.
//
// Usage in template:
//   <div x-data="chatWithPanel()" x-init="configure({tailUrl, tmuxSession})">
//
// The component expects configure() to be called with:
//   tailUrl:      API endpoint for JSONL polling (e.g. /api/chatwith/{name}/tail)
//   tmuxSession:  tmux session name for sending messages

(function () {

  document.addEventListener('alpine:init', () => {
    Alpine.data('chatWithPanel', () => ({
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

      // Configuration (set via configure())
      _tailUrl: '',
      _tmuxSession: '',

      // Screenshot injection indicator
      screenshotInjected: false,
      _screenshotTimer: null,

      // State
      state: 'waiting',  // 'waiting' | 'ready'

      configure(opts) {
        this._tailUrl = opts.tailUrl || '';
        this._tmuxSession = opts.tmuxSession || '';
        if (this._tailUrl) {
          this._startPolling();
        }
      },

      destroy() {
        if (this._pollTimer) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
        if (this._screenshotTimer) {
          clearTimeout(this._screenshotTimer);
          this._screenshotTimer = null;
        }
      },

      formatTime(ts) {
        if (!ts) return '';
        try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
      },

      toolLabel(toolId) {
        const info = this._toolMap[toolId];
        return info ? info.tool_name + ' result' : 'result';
      },

      onScroll() {
        const el = this.$refs.cwEntriesContainer;
        if (!el) return;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
        this.autoScroll = atBottom;
      },

      resumeScroll() {
        this.autoScroll = true;
        const el = this.$refs.cwEntriesContainer;
        if (el) el.scrollTop = el.scrollHeight;
      },

      async onFileSelected(event) {
        const file = event.target.files && event.target.files[0];
        if (!file) return;
        this.uploadFilename = file.name;

        // FileReader for mobile Safari compatibility (not URL.createObjectURL)
        if (file.type.startsWith('image/')) {
          const reader = new FileReader();
          reader.onload = (e) => { this.uploadPreview = e.target.result; };
          reader.readAsDataURL(file);
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
            console.warn('[chatWithPanel] upload error:', data.error);
          }
        } catch (e) {
          console.warn('[chatWithPanel] upload failed:', e);
        } finally {
          this.uploading = false;
          if (this.$refs.cwFileInput) this.$refs.cwFileInput.value = '';
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
            // Two-send strategy: bare path first for isMeta=True image injection
            const pathData = await _send(this.uploadPath);
            if (!pathData.ok) {
              console.warn('[chatWithPanel] send error (path):', pathData.error);
              return;
            }
            if (text) {
              await new Promise(r => setTimeout(r, 200));
              const textData = await _send(text);
              if (!textData.ok) {
                console.warn('[chatWithPanel] send error (text):', textData.error);
                return;
              }
            }
          } else {
            const data = await _send(text);
            if (!data.ok) {
              console.warn('[chatWithPanel] send error:', data.error);
              return;
            }
          }

          this.inputText = '';
          this.clearUpload();
          if (this.$refs.cwMessageInput) this.$refs.cwMessageInput.blur();
        } catch (e) {
          console.warn('[chatWithPanel] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      showScreenshotInjected() {
        this.screenshotInjected = true;
        if (this._screenshotTimer) clearTimeout(this._screenshotTimer);
        this._screenshotTimer = setTimeout(() => {
          this.screenshotInjected = false;
          this._screenshotTimer = null;
        }, 3000);
      },

      async _poll() {
        if (!this._tailUrl) return;
        try {
          const res = await fetch(`${this._tailUrl}?after=${this.offset}`);
          const data = await res.json();

          this.isLive = data.is_live;
          if (data.offset !== undefined) this.offset = data.offset;
          if (data.tmux_session && !this._tmuxSession) {
            this._tmuxSession = data.tmux_session;
          }

          if (data.entries && data.entries.length > 0) {
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

            if (this.autoScroll) {
              this.$nextTick(() => {
                const el = this.$refs.cwEntriesContainer;
                if (el) el.scrollTop = el.scrollHeight;
              });
            }
          }

          if (this.state === 'waiting') this.state = 'ready';

          // Stop polling if session is complete and no new entries
          if (!data.is_live && this.offset > 0 && (!data.entries || data.entries.length === 0)) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
          }
        } catch (e) {
          // Silently retry on next poll — session may not be ready yet
        }
      },

      _startPolling() {
        // Immediate first poll
        this._poll();
        // Then poll every 1.5s
        this._pollTimer = setInterval(() => this._poll(), 1500);
      },
    }));
  });
})();
