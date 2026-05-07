// Source / Context page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Handles both:
//   /graph/{id}           — full source/attachment view
//   /graph/{id}?turn=N    — context view (windowed around turn N)
//
// The template at /pages/source uses x-if to show the right layout based on
// isNote / isChat / isDoc flags, and isContext for context-specific UI.
//
// state machine: 'loading' → 'ready' | 'error'
//

(function () {
  const TYPE_BADGES = {
    note:         'bg-yellow-700',
    session:      'bg-green-700',
    conversation: 'bg-blue-700',
    status:       'bg-purple-700',
    docs:         'bg-teal-700',
    'agent-run':  'bg-orange-700',
    musing:       'bg-pink-700',
    'git-log':    'bg-gray-600',
    playbook:     'bg-indigo-700',
  };

  const CHAT_TYPES = new Set(['session', 'conversation', 'agent-run']);

  document.addEventListener('alpine:init', () => {
    Alpine.data('sourcePage', () => ({
      state: 'loading',
      errorMsg: '',

      id: '',
      src: {},
      allEntries: [],
      visibleEntries: [],
      edges: [],
      noteContent: '',
      noteMeta: null,
      noteComments: [],
      noteVersionCount: 1,
      noteProvenanceId: null,
      noteProvenanceType: null,
      attachments: [],
      unrefAttachments: [],
      highlightId: '',
      zoom: localStorage.getItem('mdZoom') || 'compact',
      mdSizes: { compact: '0.75rem', normal: '0.875rem', expanded: '1rem' },
      setZoom: function(level) {
        this.zoom = level;
        localStorage.setItem('mdZoom', level);
      },

      // Context mode
      isContext: false,
      targetTurn: 0,
      contextWindow: 5,

      // Rich-content note state
      isRichContent: false,
      _richResolveData: null,
      showingRichHtml: true,

      // Type flags (computed after fetch)
      srcType: '',
      badgeCls: '',
      isNote: false,
      isChat: false,
      isDoc: false,
      date: '',

      _badgeClsFor(type) {
        return TYPE_BADGES[type] || 'bg-gray-700';
      },

      // Display title: notes now store a clean (no leading `#`) title at write
      // time, so we trust src.title verbatim and only fall back to "Untitled".
      get displayTitle() {
        return this.src?.title || 'Untitled';
      },

      // One- or two-sentence purpose of the note. Set explicitly on write
      // (via --short-description on the CLI / payload) or backfilled by the
      // Update Title & Summary action; null when neither has run.
      get shortDescription() {
        return this.src?.short_description || '';
      },

      // Border color by source type
      get borderColor() {
        const type = this.srcType;
        if (type === 'note') return '#eab308';
        if (type === 'session') return '#22c55e';
        if (type === 'thought') return '#8b5cf6';
        if (type === 'docs') return '#3b82f6';
        return '#64748b';
      },

      // Tags from metadata
      get tags() {
        try {
          const meta = typeof this.src?.metadata === 'string' ? JSON.parse(this.src.metadata) : (this.src?.metadata || {});
          return meta.tags || [];
        } catch { return []; }
      },

      // Author from metadata
      get author() {
        try {
          const meta = typeof this.src?.metadata === 'string' ? JSON.parse(this.src.metadata) : (this.src?.metadata || {});
          return meta.author || '';
        } catch { return ''; }
      },

      // Note version
      get noteVersion() {
        if (!this.isNote) return null;
        return this.noteVersionCount > 1 ? this.noteVersionCount : null;
      },

      // Comment count
      get commentCount() { return this.noteComments?.length || 0; },

      // Provenance link URL
      get provenanceLink() {
        if (!this.noteProvenanceId) return '';
        return '/' + (this.noteProvenanceType === 'bead' ? 'bead' : 'graph') + '/' + this.noteProvenanceId;
      },

      // Has any secondary metadata to show in row 3
      get authorOrMeta() { return !!(this.author || this.commentCount || this.provenanceLink); },

      // ── Header metadata strip (turns · range · duration · tokens) ──
      // Chat-only. All getters return null/false for non-chat or empty
      // sources so the wrapper template's x-if collapses cleanly.
      //
      // Defense-in-depth: prefer authoritative ``source.metadata`` fields
      // (``total_turns``, ``started_at``, ``ended_at``, token counts) over
      // values derived from ``allEntries``. The entries list can be a
      // sliced view (context-mode windowing, future caps) — metadata
      // reflects the full source. Falls back to entry-derived values for
      // non-session sources where these fields are absent.

      get _sourceMeta() {
        try {
          if (typeof this.src?.metadata === 'string') return JSON.parse(this.src.metadata);
          return this.src?.metadata || {};
        } catch { return {}; }
      },

      get turnsCount() {
        if (!this.isChat) return null;
        const meta = this._sourceMeta;
        const t = meta?.total_turns;
        if (Number.isFinite(t) && t > 0) return t;
        if (!this.allEntries.length) return null;
        return this.allEntries.length;
      },

      _entryDates() {
        if (!this.isChat || !this.allEntries.length) return [];
        const out = [];
        for (const e of this.allEntries) {
          if (!e || !e.created_at) continue;
          const d = new Date(e.created_at);
          if (!isNaN(d.getTime())) out.push(d);
        }
        return out;
      },

      get startAt() {
        const meta = this._sourceMeta;
        const s = meta?.started_at;
        if (typeof s === 'string' && s) {
          const d = new Date(s);
          if (!isNaN(d.getTime())) return d.toISOString();
        }
        const dates = this._entryDates();
        if (!dates.length) return null;
        const ms = Math.min(...dates.map(d => d.getTime()));
        return new Date(ms).toISOString();
      },

      get endAt() {
        const meta = this._sourceMeta;
        const e = meta?.ended_at;
        if (typeof e === 'string' && e) {
          const d = new Date(e);
          if (!isNaN(d.getTime())) return d.toISOString();
        }
        const dates = this._entryDates();
        if (!dates.length) return null;
        const ms = Math.max(...dates.map(d => d.getTime()));
        return new Date(ms).toISOString();
      },

      get durationMs() {
        if (!this.startAt || !this.endAt) return null;
        return new Date(this.endAt).getTime() - new Date(this.startAt).getTime();
      },

      get durationFormatted() {
        const d = this.durationMs;
        if (d == null) return null;
        if (d < 60000) return null;                       // < 60s → omit
        if (d < 3600000) return `${Math.floor(d / 60000)}m`;
        if (d < 86400000) {
          const h = Math.floor(d / 3600000);
          const m = Math.floor((d - h * 3600000) / 60000);
          return `${h}h ${m}m`;
        }
        const days = Math.floor(d / 86400000);
        const h = Math.floor((d - days * 86400000) / 3600000);
        return `${days}d ${h}h`;
      },

      get timeRangeFormatted() {
        if (!this.isChat) return null;
        // Single-entry chat: nothing to range over. Prefer metadata's
        // total_turns when available so a viewer that's currently
        // showing a sliced/empty entries list still hides the range.
        const turns = this.turnsCount;
        if (turns != null && turns < 2) return null;
        if (turns == null && this.allEntries.length < 2) return null;
        if (!this.startAt || !this.endAt) return null;
        const start = new Date(this.startAt);
        const end = new Date(this.endAt);
        if (isNaN(start.getTime()) || isNaN(end.getTime())) return null;
        const sameDay = (
          start.getFullYear() === end.getFullYear() &&
          start.getMonth() === end.getMonth() &&
          start.getDate() === end.getDate()
        );
        const within24h = (end.getTime() - start.getTime()) < 86400000;
        // Format time and date separately and join with a space so the
        // output stays "May 4 14:23" across locales — Intl's combined
        // medium format inserts locale-specific punctuation (e.g.
        // "Apr 28, 12:00" in en-US) which doesn't match the spec.
        const timeFmt = new Intl.DateTimeFormat(undefined, {
          hour: '2-digit', minute: '2-digit', hour12: false,
        });
        if (sameDay && within24h) {
          return `${timeFmt.format(start)} → ${timeFmt.format(end)}`;
        }
        const dateFmt = new Intl.DateTimeFormat(undefined, {
          month: 'short', day: 'numeric',
        });
        const fmtBoth = (d) => `${dateFmt.format(d)} ${timeFmt.format(d)}`;
        return `${fmtBoth(start)} → ${fmtBoth(end)}`;
      },

      get tokenEstimate() {
        if (!this.isChat) return null;
        // Authoritative source: metadata's total_input_tokens +
        // total_output_tokens (set at ingest from the underlying chat).
        // Fall back to a char-based estimate over allEntries when
        // metadata is absent (non-session sources, partial ingests).
        const meta = this._sourceMeta;
        const ti = Number(meta?.total_input_tokens) || 0;
        const to = Number(meta?.total_output_tokens) || 0;
        if (ti + to > 0) return ti + to;
        if (!this.allEntries.length) return null;
        let total = 0;
        for (const e of this.allEntries) {
          if (e && typeof e.content === 'string') total += e.content.length;
        }
        if (total <= 0) return null;
        return Math.ceil(total / 4);
      },

      get tokensFormatted() {
        const n = this.tokenEstimate;
        if (n == null) return null;
        if (n < 1000) return `~${n} tokens`;
        if (n < 1_000_000) return `~${(n / 1000).toFixed(1)}k tokens`;
        return `~${(n / 1_000_000).toFixed(1)}M tokens`;
      },

      get hasMeta() {
        if (!this.isChat) return false;
        if (!this.allEntries.length && !this.turnsCount) return false;
        return !!(
          this.turnsCount ||
          this.timeRangeFormatted ||
          this.durationFormatted ||
          this.tokensFormatted
        );
      },

      // Copy graph:// link to clipboard with visual feedback
      copyGraphLink() {
        navigator.clipboard.writeText('graph://' + (this.src.id || '').slice(0, 12));
        const btn = this.$el;
        const origText = (this.src.id || '').slice(0, 12);
        btn.textContent = 'copied!';
        btn.style.color = '#34d399';
        setTimeout(() => {
          btn.textContent = origText;
          btn.style.color = '#3d4f63';
        }, 1000);
      },

      _updateVisibleEntries() {
        if (!this.isContext) {
          this.visibleEntries = this.allEntries;
        } else {
          this.visibleEntries = this.allEntries.filter(
            e => e.turn_number != null && Math.abs(e.turn_number - this.targetTurn) <= this.contextWindow
          );
        }
      },

      async showMoreContext() {
        this.contextWindow += 5;
        const url = `/graph/${this.id}?turn=${this.targetTurn}&window=${this.contextWindow}`;
        history.replaceState({}, '', url);
        try {
          const res = await fetch(`/api/graph/${this.id}?turn=${this.targetTurn}&window=${this.contextWindow}`);
          const data = await res.json();
          if (data && !data.error && Array.isArray(data.entries)) {
            this.allEntries = data.entries.map((e, i) => ({
              ...e,
              _key: e.turn_number != null ? `turn-${e.turn_number}` : `entry-${i}`,
            }));
          }
        } catch (_) {
          // Refetch is best-effort — fall back to whatever we already have.
        }
        this._updateVisibleEntries();
      },

      isHighlighted(turnNum) {
        return this.isContext && turnNum == this.targetTurn;
      },

      edgeTarget(edge) {
        return edge.source_id === this.src.id ? edge.target_id : edge.source_id;
      },

      edgeTargetType(edge) {
        return edge.source_id === this.src.id ? edge.target_type : edge.source_type;
      },

      edgeMeta(edge) {
        let meta;
        try {
          meta = typeof edge.metadata === 'string' ? JSON.parse(edge.metadata || '{}') : (edge.metadata || {});
        } catch (_) {
          meta = {};
        }
        const turns = meta.turns
          ? ` t${meta.turns.from}${meta.turns.to !== meta.turns.from ? '-' + meta.turns.to : ''}`
          : '';
        const note = meta.note ? ` — ${meta.note.slice(0, 50)}` : '';
        return turns + note;
      },

      edgeHref(edge) {
        const other = this.edgeTarget(edge);
        const otherType = this.edgeTargetType(edge);
        return `/${otherType === 'source' ? 'graph' : 'bead'}/${other}`;
      },

      // Attachment fields (when type === 'attachment')
      isAttachment: false,
      attData: null,

      async init() {
        const path = window.location.pathname;
        const m = path.match(/^\/(graph|source)\/(.+)$/);
        this.id = m ? m[2] : '';
        if (!this.id) {
          this.errorMsg = 'No source ID in URL';
          this.state = 'error';
          return;
        }

        const params = new URLSearchParams(window.location.search);
        const turn = params.get('turn');
        if (turn) {
          this.isContext = true;
          this.targetTurn = parseInt(turn, 10);
          this.contextWindow = parseInt(params.get('window') || '5', 10);
        }
        this.highlightId = params.get('highlight') || '';

        try {
          const url = this.isContext
            ? `/api/graph/${this.id}?turn=${this.targetTurn}&window=${this.contextWindow}`
            : `/api/graph/${this.id}`;
          const res = await fetch(url);
          const data = await res.json();

          if (data && data.error) {
            this.errorMsg = data.error;
            this.state = 'error';
            return;
          }

          // Comment response — redirect to parent source with highlight
          // Use replaceState (not navigateTo/pushState) so the intermediate
          // /graph/{comment_id} URL doesn't remain in history — avoids back-button loop.
          if (data.type === 'comment') {
            history.replaceState({}, '', data.redirect);
            route();
            return;
          }

          // Attachment response
          if (data.type === 'attachment') {
            this.isAttachment = true;
            this.attData = data;
            this.state = 'ready';
            const titleEl = document.getElementById('page-title');
            if (titleEl) titleEl.textContent = `Attachment: ${data.filename}`;
            return;
          }

          this.src = data.source || {};
          this.allEntries = (data.entries || []).map((e, i) => ({
            ...e,
            _key: e.turn_number != null ? `turn-${e.turn_number}` : `entry-${i}`,
          }));
          this.edges = (data.edges || []).slice(0, 20).map((e, i) => ({ ...e, _key: `edge-${i}` }));

          this.srcType = this.src.type || 'unknown';
          this.badgeCls = this._badgeClsFor(this.srcType);
          this.isNote = this.srcType === 'note';
          this.isChat = CHAT_TYPES.has(this.srcType);
          this.isDoc = !this.isNote && !this.isChat;
          // 24-hour date+time: strip T/Z, keep to-minute resolution.
          this.date = ((this.src.created_at || '').replace('T', ' ').replace('Z', '')).slice(0, 16);
          this.noteContent = this.isNote ? (this.allEntries[0]?.content || '') : '';
          // Strip first heading from note body (it's shown in the header now)
          if (this.isNote && this.noteContent) {
            this.noteContent = this.noteContent.replace(/^#+\s+.+\n?/, '');
          }

          if (this.isNote) {
            const raw = this.src.metadata;
            this.noteMeta = typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {});
            this.noteComments = (data.comments || []).map((c, i) => ({ ...c, _key: `comment-${i}` }));
            this.noteVersionCount = data.version_count || 1;
            // Find provenance link from edges (either direction)
            const provEdge = this.edges.find(e => e.relation === 'conceived_at');
            if (provEdge) {
              const isSource = provEdge.source_id === this.src.id;
              this.noteProvenanceId = isSource ? provEdge.target_id : provEdge.source_id;
              this.noteProvenanceType = isSource ? provEdge.target_type : provEdge.source_type;
            }
          }

          // Fetch attachments for notes
          if (this.isNote) {
            try {
              const attRes = await fetch(`/api/source/${this.id}/attachments`);
              const attData = await attRes.json();
              this.attachments = attData.attachments || [];
              this.unrefAttachments = this.attachments.filter(
                a => !this.noteContent.includes('graph://' + a.id.slice(0, 12))
                     && !this.noteContent.includes('graph://' + a.id)
                     && !(a.source_id && a.source_id.includes('@'))
              );
            } catch (_) {
              // Non-critical — just skip attachment list
            }
          }

          // Detect rich-content note — render via shared _renderEmbed after DOM ready
          if (this.isNote && this.noteMeta && this.noteMeta.rich_content) {
            this.isRichContent = true;
            try {
              const resolveRes = await fetch('/api/resolve/' + encodeURIComponent(this.id));
              if (resolveRes.ok) {
                this._richResolveData = await resolveRes.json();
                this._richResolveData._directView = true;
              }
            } catch (_) {
              // Fall back to markdown rendering
            }
          }

          this._updateVisibleEntries();
          this.state = 'ready';

          // Render rich-content via shared embed renderer (same as ![[id]] path)
          if (this.isRichContent && this._richResolveData) {
            this.$nextTick(() => {
              const container = document.querySelector('[data-testid="rich-content-container"]');
              if (container && window.renderRichEmbed) {
                window.renderRichEmbed(container, this._richResolveData);
              }
            });
          }

          // Update page title
          const titleEl = document.getElementById('page-title');
          if (titleEl) {
            titleEl.textContent = this.isContext
              ? `${(this.src.title || this.id).slice(0, 40)} — turn ${this.targetTurn}`
              : `Source: ${this.id.slice(0, 12)}`;
          }

          // Scroll to target turn in context mode
          if (this.isContext) {
            this.$nextTick(() => {
              const el = document.getElementById(`turn-${this.targetTurn}`);
              if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            });
          }

          // Scroll to highlighted comment (from ?highlight= or #comment-)
          this.$nextTick(() => {
            var hash = window.location.hash;
            var highlight = new URLSearchParams(window.location.search).get('highlight');
            var target = hash ? hash.slice(1) : (highlight ? 'comment-' + highlight : null);
            if (target) {
              var el = document.getElementById(target);
              if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.classList.add('ring-2', 'ring-indigo-500', 'bg-indigo-900/20', 'rounded');
                setTimeout(() => el.classList.remove('ring-2', 'ring-indigo-500', 'bg-indigo-900/20'), 5000);
              }
            }
          });
        } catch (e) {
          this.errorMsg = e.message || 'Failed to load source';
          this.state = 'error';
        }
      },

      destroy() {},
    }));
  });
})();
