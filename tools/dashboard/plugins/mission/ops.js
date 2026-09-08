/* Ops is a projection of records, not a second tracker or an execution queue. */
(function (global) {
  'use strict';
  const enc = encodeURIComponent;
  const phases = {designing: ['Designing', 20], implementing: ['Implementing', 45],
    debugging: ['Debugging', 55], testing: ['Testing', 70], verifying: ['Verifying', 85],
    waiting: ['Waiting', 0], unknown: ['Phase unknown', 0]};
  function sessionId(value) {
    const id = String(value || '').replace(/^(session:|terminal:)/, '');
    return /^(auto|host)-[a-zA-Z0-9-]+$/.test(id) ? id : '';
  }
  function safeHref(value) {
    if (typeof value !== 'string' || !value || value.length > 2048 || /[\x00-\x20\x7f\\]/.test(value) || value.startsWith('//')) return '';
    if (/^graph:\/\/[a-zA-Z0-9-]+$/.test(value)) return value;
    if (/^\/(design\/|bead\/|session\/|mission\/|api\/session\/[^/]+\/output\/)/.test(value)) return value;
    try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? value : ''; }
    catch (_) { return ''; }
  }
  function refHref(ref) {
    if (typeof ref !== 'string') return '';
    if (ref.startsWith('bead:')) return safeHref('/bead/' + enc(ref.slice(5)));
    if (ref.startsWith('design:')) return safeHref('/design/' + enc(ref.slice(7)));
    if (ref.startsWith('graph:') && !ref.startsWith('graph://')) return safeHref('graph://' + ref.slice(6));
    return safeHref(ref);
  }
  function stamp(value) {
    if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value).toISOString();
    if (typeof value !== 'string' || !value) return null;
    // Dolt DATETIME is UTC but has no zone in its textual projection.
    return /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(value) ? value.replace(' ', 'T') + 'Z' : value;
  }
  global.missionOps = function (config) {
    return {
      config, view: 'overview', filter: 'focus', search: '', limit: 100, selectedId: '',
      issues: [], sessions: [], seats: [], raw: null, errors: [], loading: true,
      drafts: {}, answers: {}, sending: false, sendError: '', notice: '', now: Date.now(), clock: null,
      dictationHint: false, reportTitle: '', reportBody: '',
      filters: [['focus', 'In focus'], ['all', 'All open'], ['ready', 'Ready to pick up'],
        ['spec', 'Needs shaping'], ['check', 'Ready to check'], ['completed', 'Recently completed']],
      init() { this.refresh(); this.clock = setInterval(() => { this.now = Date.now(); }, 60000); },
      destroy() { clearInterval(this.clock); },
      async refresh() {
        if (this._fetching) return;
        this._fetching = true; this.loading = true;
        try {
          const r = await fetch('/api/mission/ops/' + enc(config.mission_id), {credentials: 'same-origin'});
          if (!r.ok) throw new Error('Cannot load Ops (' + r.status + ').');
          const data = await r.json();
          if (!Array.isArray(data.tasks) || !Array.isArray(data.items) || !Array.isArray(data.sessions)) throw new Error('Invalid Ops response.');
          this.now = Date.now();
          this.raw = data; this.errors = data.errors || []; this.sessions = data.sessions;
          this.project();
          if (this.view === 'detail' && !this.current) this.view = 'overview';
        } catch (e) { this.errors = [{source: 'ops', message: e.message + (this.raw ? ' Previous records remain visible.' : '')}]; }
        finally { this.loading = false; this._fetching = false; }
      },
      sessionHref(id) { return '/session/' + enc(config.org) + '/' + enc(id); },
      refHref,
      people(ids) {
        return [...new Set(ids.filter(Boolean))].map(id => {
          const live = this.sessions.find(s => s.id === id);
          return [id.slice(0, 2).toUpperCase(), id, live ? 'Assigned session · live' : 'Assigned session · not in live roster', id];
        });
      },
      project() {
        const data = this.raw, tasks = new Map(data.tasks.map(t => [t.id, t]));
        const missions = new Map((data.missions || []).map(m => [m.mission_id, m]));
        const pillars = data.pillars || [];
        const live = new Set(this.sessions.map(s => s.id));
        this.seats = pillars.map(p => ({key: p.mission_id + ':' + p.pillar_id,
          label: (missions.get(p.mission_id)?.name || p.mission_id) + ' · ' + p.name,
          session: p.coordinator_session || '',
          status: !p.coordinator_session ? 'Vacant' : this.errors.some(e => e.source === 'sessions') ? 'Liveness unknown' : live.has(p.coordinator_session) ? 'Live coordinator' : 'Coordinator not in live roster'}));
        const base = (id, headline) => ({id, headline, subtitle: '', phase: 'Phase unknown', stageWidth: 0,
          tone: 'neutral', people: [], refs: [], artifacts: [], changes: [], options: [], background: '',
          impact: '', recommendation: '', question: '', updatedAt: null, completedAt: null,
          group: 'spec', focus: false, canSend: false, canResolve: false, recipientLabel: '', priority: 2});
        const work = data.tasks.map(t => {
          const meta = t.mission_ops || {}, issue = base('task:' + t.id, meta.headline || t.title || t.id);
          issue.task = t; issue.priority = Number.isFinite(t.priority) ? t.priority : 2;
          issue.people = this.people([sessionId(t.assignee)]);
          issue.focus = t.status !== 'closed' && issue.people.some(p => live.has(p[3]));
          const mid = t.mission_ids.find(m => missions.has(m));
          const pillar = pillars.find(p => p.mission_id === mid && (p.bead_labels || []).some(lb => t.pillar_labels.includes(lb)));
          issue.missionId = mid || ''; issue.pillarId = pillar?.pillar_id || '';
          issue.canSend = !!pillar?.coordinator_session;
          issue.recipientLabel = pillar ? 'Feedback to ' + pillar.name + ' coordinator: ' + (pillar.coordinator_session || 'vacant') : 'No mission pillar owns this work.';
          const allocation = t.mission_ids.length ? t.mission_ids.map(m => missions.get(m)?.name || 'Unresolved mission: ' + m).join(' · ') : 'Unallocated';
          issue.subtitle = meta.subtitle || allocation;
          issue.updatedAt = stamp(t.updated_at); issue.completedAt = stamp(t.closed_at);
          issue.refs = ['bead:' + t.id]; issue.artifacts = [{label: 'Open task', href: '/bead/' + enc(t.id)}];
          issue.background = 'Open the task for its full specification.';
          issue.changes = ['Tracker status: ' + t.status, 'Readiness: ' + t.readiness,
            'Assignee: ' + (t.assignee || 'unassigned'), 'Allocation: ' + allocation,
            'Prerequisites: ' + (t.dependencies_known ? (t.blocks_on.join(', ') || 'none recorded') : 'unknown')];
          issue.group = t.status === 'closed' ? 'completed' : t.readiness === 'ready' ? 'ready' : 'spec';
          if (t.status === 'closed') { issue.phase = 'Closed'; issue.stageWidth = 100; issue.tone = 'done'; }
          else if (meta.phase && meta.phase_at && meta.reported_by) {
            const age = this.now - Date.parse(meta.phase_at);
            if (age >= 0 && age <= 86400000 && phases[meta.phase]) {
              [issue.phase, issue.stageWidth] = phases[meta.phase];
              issue.tone = meta.phase === 'waiting' ? 'wait' : '';
              if (meta.phase === 'verifying') issue.group = 'check';
            } else { issue.phase = 'Phase report stale'; issue.stageWidth = 0; }
          }
          return issue;
        });
        const questions = data.items.filter(i => i.kind === 'question' && i.state === 'open' && !i.retired).map(i => {
          const b = i.briefing || {}, issue = base('question:' + i.mission_id + ':' + i.key, i.title);
          const pillar = pillars.find(p => p.mission_id === i.mission_id && p.pillar_id === i.surface_id);
          const linked = (i.refs || []).filter(r => r.startsWith('bead:')).map(r => tasks.get(r.slice(5))).filter(Boolean);
          issue.item = i; issue.missionId = i.mission_id; issue.pillarId = i.surface_id;
          issue.question = i.ask || ''; issue.background = i.body || 'Background has not been supplied. Ask for clarification.';
          issue.subtitle = b.subtitle || (missions.get(i.mission_id)?.name || '') + (pillar ? ' · ' + pillar.name : '');
          issue.impact = b.impact || ''; issue.recommendation = b.recommendation || '';
          issue.options = Array.isArray(b.options) ? b.options : [];
          issue.artifacts = (b.artifacts || []).filter(a => safeHref(a.href));
          issue.refs = [...(i.refs || []), ...(b.source_refs || [])];
          for (const ref of issue.refs) {
            const href = refHref(ref);
            if (href && (/^graph:|^\/design\//.test(href)) && !issue.artifacts.some(a => a.href === href)) issue.artifacts.push({label: ref, href});
          }
          issue.people = this.people([sessionId(i.asked_by), sessionId(i.owner), ...linked.map(t => sessionId(t.assignee))]);
          issue.recipientLabel = 'Replies go to ' + (pillar?.name || i.surface_id) + ': ' + (pillar?.coordinator_session || 'no coordinator; stored only');
          issue.canSend = true; issue.canResolve = !!issue.question;
          issue.phase = i.blocking ? 'Decision needed' : 'Open question'; issue.tone = 'wait'; issue.group = 'attention';
          issue.focus = missions.get(i.mission_id)?.status !== 'complete';
          issue.updatedAt = stamp(i.updated_at || i.asked_at);
          issue.priority = linked.length ? Math.min(...linked.map(t => Number.isFinite(t.priority) ? t.priority : 2)) : 2;
          issue.changes = (i.discussion || []).map(d => (d.by || '') + ' · ' + (d.at || '') + '\n' + d.text);
          return issue;
        });
        this.issues = [...questions, ...work].sort((a, b) =>
          Number(!!b.item?.blocking) - Number(!!a.item?.blocking) || a.priority - b.priority ||
          (Date.parse(b.updatedAt) || 0) - (Date.parse(a.updatedAt) || 0) || a.id.localeCompare(b.id));
      },
      get current() { return this.issues.find(i => i.id === this.selectedId) || null; },
      get featured() { return this.issues.find(i => i.question && i.focus); },
      get scopeLabel() { return this.raw ? this.raw.org + ' · ' + this.raw.tasks.length + ' tasks · ' + this.sessions.length + ' live sessions · refreshed ' + this.ago(this.raw.generated_at) : 'Reading organization records'; },
      matches(issue, group) { return group === 'focus' ? issue.focus : group === 'all' ? issue.group !== 'completed' : issue.group === group; },
      count(group) { return this.issues.filter(i => this.matches(i, group)).length; },
      get filteredRows() {
        let rows = this.issues.filter(i => this.matches(i, this.filter));
        const q = this.search.trim().toLowerCase();
        if (q) rows = rows.filter(i => [i.headline, i.subtitle, ...i.people.map(p => p[3]), ...i.refs].join(' ').toLowerCase().includes(q));
        return this.filter === 'completed' ? rows.sort((a, b) => (Date.parse(b.completedAt) || 0) - (Date.parse(a.completedAt) || 0)) : rows;
      },
      get rows() { return this.filteredRows.slice(0, this.limit); },
      get unlinkedSessions() { const ids = new Set(this.issues.filter(i => i.task && i.group !== 'completed').flatMap(i => i.people.map(p => p[3]))); return this.sessions.filter(s => !ids.has(s.id)); },
      get reportPillar() { return (this.raw?.pillars || []).find(p => p.mission_id === config.mission_id && p.pillar_id === 'mc-infra' && p.coordinator_session) || null; },
      get reportRecipient() { return this.reportPillar ? 'Send to Mission Control Infrastructure · ' + this.reportPillar.coordinator_session : 'No Mission Control Infrastructure coordinator is connected for this mission.'; },
      get draft() { return this.drafts[this.selectedId] || ''; },
      set draft(value) { this.drafts[this.selectedId] = value; },
      ago(at) { const ms = this.now - Date.parse(stamp(at)); if (!at || !Number.isFinite(ms)) return 'Unknown'; if (ms < 0) return 'Clock mismatch'; const m = Math.floor(ms / 60000); return m < 1 ? 'Just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.floor(m / 60) + ' hr ago' : Math.floor(m / 1440) + ' days ago'; },
      timeTitle(i) { const at = i.completedAt || i.updatedAt; return at ? new Date(at).toUTCString() : 'Source timestamp unavailable'; },
      async open(id) {
        this.selectedId = id; this.view = 'detail'; this.sendError = ''; this.dictationHint = false;
        const issue = this.current;
        if (!issue?.task || !issue.missionId || issue.detailLoaded) return;
        try {
          const r = await fetch('/api/mission/tasks/' + enc(issue.missionId) + '/detail?ids=' + enc(issue.task.id));
          if (!r.ok) throw new Error();
          const detail = (await r.json()).tasks?.[issue.task.id];
          if (!detail) throw new Error();
          issue.background = detail.desc || 'No task description supplied.';
          issue.changes.push(...(detail.comments || []).map(c => c.by + ' · ' + c.at + '\n' + c.text));
          if (detail.evidence) issue.changes.push(detail.evidence);
          issue.detailLoaded = true;
        } catch (_) { issue.background = 'Task details could not be loaded. Open the task directly or retry.'; }
      },
      back() { this.view = 'overview'; this.sendError = ''; },
      report() { this.view = 'report'; this.sendError = ''; },
      dictate() { this.dictationHint = true; this.$nextTick(() => this.$refs.answer?.focus()); },
      edit() { delete this.answers[this.selectedId]; },
      async copy() { try { await navigator.clipboard.writeText(this.answers[this.selectedId]?.text || this.draft); this.notice = 'Copied'; } catch (_) { this.sendError = 'Copy unavailable; select the text to copy.'; } },
      endpoint(issue, action) {
        const root = '/api/mission/';
        return issue.item ? root + 'item/' + enc(issue.missionId) + '/' + enc(issue.pillarId) + '/' + enc(issue.item.item_id) + '/' + action
          : root + 'chat/' + enc(issue.missionId) + '/' + enc(issue.pillarId);
      },
      async post(url, text) {
        const r = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
        if (!r.ok) throw new Error('Not saved (' + r.status + '). Your draft is still here.');
        const out = await r.json();
        if (!out.ok) throw new Error('Save was not confirmed. Your draft is still here.');
        return out.relayed ? 'Saved; relay confirmed' : 'Saved; relay not confirmed';
      },
      async submit(action = 'reply', override = null) {
        const issue = this.current, text = override === null ? this.draft.trim() : override;
        if (!issue?.canSend || !text || this.sending || (action === 'answer' && !issue.canResolve)) return;
        const id = issue.id;
        this.sending = true; this.sendError = '';
        try {
          const payload = issue.item ? text : '[' + issue.task.id + '] ' + text;
          const receipt = await this.post(this.endpoint(issue, action), payload);
          this.answers[id] = {text, receipt};
          this.notice = receipt;
          await this.$nextTick();
          this.drafts[id] = '';
          if (action === 'answer') { issue.canResolve = false; issue.canSend = false; }
          await this.refresh();
        } catch (e) { this.sendError = e.message; }
        finally { this.sending = false; }
      },
      clarify() { const text = this.draft.trim() || 'Please refine this item: explain the concrete background, link the design or plan, and state one answerable question with options and consequences.'; this.draft = text; return this.submit('reply', '[Needs clarification] ' + text); },
      resolve() { if (this.current?.canResolve && this.draft.trim() && global.confirm('Record this text as the resolution and mark this question answered?')) return this.submit('answer'); },
      async addReport() {
        if (!this.reportPillar || !this.reportTitle.trim() || !this.reportBody.trim() || this.sending) return;
        this.sending = true; this.sendError = '';
        try {
          this.notice = await this.post('/api/mission/chat/' + enc(config.mission_id) + '/mc-infra', this.reportTitle.trim() + '\n\n' + this.reportBody.trim());
          await this.$nextTick();
          this.reportTitle = ''; this.reportBody = ''; this.view = 'overview';
          await this.refresh();
        } catch (e) { this.sendError = e.message; }
        finally { this.sending = false; }
      }
    };
  };
})(window);
