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
      mdSizes: { compact: '0.875rem', normal: '1rem', expanded: '1.125rem' },
      setZoom: function(level) {
        this.zoom = level;
        localStorage.setItem('mdZoom', level);
      },

      // Context mode
      isContext: false,
      targetTurn: 0,
      contextWindow: 5,

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

      // Display title: extract first # heading from content for notes, strip markdown prefix
      get displayTitle() {
        if (!this.isNote) return this.src?.title || 'Untitled';
        // For notes, title field may contain raw markdown heading
        const title = this.src?.title || '';
        const match = title.match(/^#+\s+(.+)/);
        if (match) return match[1];
        // Fallback: try first line of content
        if (!title && this.noteContent) {
          const contentMatch = this.noteContent.match(/^#+\s+(.+)/m);
          if (contentMatch) return contentMatch[1];
        }
        return title || 'Untitled';
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

      showMoreContext() {
        this.contextWindow += 5;
        const url = `/graph/${this.id}?turn=${this.targetTurn}&window=${this.contextWindow}`;
        history.replaceState({}, '', url);
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
          const res = await fetch(`/api/graph/${this.id}`);
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
          this.date = (this.src.created_at || '').slice(0, 10);
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
              );
            } catch (_) {
              // Non-critical — just skip attachment list
            }
          }

          this._updateVisibleEntries();
          this.state = 'ready';

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
