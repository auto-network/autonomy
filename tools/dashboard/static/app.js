// Autonomy Dashboard — client-side routing and rendering
// Every view is a function that fetches JSON from the API and renders it.

const content = document.getElementById('content');
const pageTitle = document.getElementById('page-title');
const statsSummary = document.getElementById('stats-summary');
const globalSearch = document.getElementById('global-search');

// ── Markdown Rendering ───────────────────────────────────────

function renderMd(md) {
  const html = marked.parse(md || '');
  const el = document.createElement('div');
  el.className = 'markdown-body';
  el.innerHTML = html;
  el.querySelectorAll('pre code').forEach(block => hljs.highlightElement(block));
  return el;
}

// ── API Helpers ──────────────────────────────────────────────

async function api(path) {
  const res = await fetch(path);
  return res.json();
}

// ── Badge Helpers ────────────────────────────────────────────

function priorityBadge(p) {
  return `<span class="badge badge-p${p}">P${p}</span>`;
}

function statusBadge(s) {
  const cls = s === 'closed' ? 'closed' : s === 'in_progress' ? 'in_progress' : s === 'blocked' ? 'blocked' : 'open';
  return `<span class="badge badge-${cls}">${s}</span>`;
}

// ── Pages ────────────────────────────────────────────────────

async function renderBeads() {
  pageTitle.textContent = 'Beads';
  const data = await api('/api/beads/list');
  if (data.error) {
    content.innerHTML = `<div class="text-red-400">${data.error}</div>`;
    return;
  }
  const issues = Array.isArray(data) ? data : [];

  // Group by status
  const ready = issues.filter(i => i.status === 'open' && !i.dependencies?.some(d => d.status !== 'closed'));
  const inProgress = issues.filter(i => i.status === 'in_progress');
  const closed = issues.filter(i => i.status === 'closed');
  const blocked = issues.filter(i => i.status === 'open' && i.dependencies?.some(d => d.status !== 'closed'));

  function renderIssueRow(issue) {
    const type = issue.issue_type === 'epic' ? '📦' : issue.issue_type === 'bug' ? '🐛' : '📋';
    return `
      <div class="flex items-center gap-3 p-3 bg-gray-800 rounded-lg hover:bg-gray-750 cursor-pointer border border-gray-700"
           onclick="navigateTo('/bead/${issue.id}')">
        <span>${type}</span>
        <span class="font-mono text-xs text-gray-500">${issue.id}</span>
        ${priorityBadge(issue.priority)}
        <span class="flex-1 truncate">${issue.title}</span>
        ${statusBadge(issue.status)}
      </div>`;
  }

  function renderSection(title, items, defaultOpen = true) {
    if (!items.length) return '';
    return `
      <details ${defaultOpen ? 'open' : ''} class="mb-6">
        <summary class="text-lg font-semibold mb-3 cursor-pointer">${title} <span class="text-gray-500">(${items.length})</span></summary>
        <div class="space-y-2">${items.map(renderIssueRow).join('')}</div>
      </details>`;
  }

  content.innerHTML =
    renderSection('In Progress', inProgress) +
    renderSection('Ready', ready) +
    renderSection('Blocked', blocked, false) +
    renderSection('Closed', closed, false);
}

async function renderBeadDetail(id) {
  pageTitle.textContent = `Bead: ${id}`;
  const data = await api(`/api/bead/${id}`);
  if (data.error) {
    content.innerHTML = `<div class="text-red-400">${data.error}</div>`;
    return;
  }
  const bead = Array.isArray(data) ? data[0] : data;
  if (!bead) {
    content.innerHTML = '<div class="text-gray-400">Bead not found</div>';
    return;
  }

  // Also fetch primer
  const primer = await api(`/api/primer/${id}`);

  let html = `
    <div class="mb-6">
      <h1 class="text-2xl font-bold mb-2">${bead.title}</h1>
      <div class="flex gap-2 mb-4">
        <span class="font-mono text-sm text-gray-500">${bead.id}</span>
        ${priorityBadge(bead.priority)}
        ${statusBadge(bead.status)}
        <span class="text-sm text-gray-400">${bead.issue_type}</span>
      </div>
    </div>`;

  if (primer.content) {
    html += '<div class="mt-6" id="primer-content"></div>';
  }

  content.innerHTML = html;

  // Render primer as markdown
  if (primer.content) {
    document.getElementById('primer-content').appendChild(renderMd(primer.content));
  }
}

let sessionsInterval = null;

async function renderSessions() {
  pageTitle.textContent = 'Sessions';
  if (sessionsInterval) clearInterval(sessionsInterval);

  async function refresh() {
    const active = await api('/api/active?threshold=600');
    const historical = await api('/api/sources?type=session&limit=20');

    let html = '';

    // Active sessions
    if (Array.isArray(active) && active.length > 0) {
      html += '<h2 class="text-lg font-semibold mb-3 text-green-400">● Active Sessions</h2>';
      html += '<div class="space-y-2 mb-8">';
      for (const s of active) {
        const ageStr = s.age_seconds < 60 ? `${s.age_seconds}s ago` : `${Math.round(s.age_seconds/60)}m ago`;
        const sizeStr = (s.size_bytes / 1024 / 1024).toFixed(1) + ' MB';
        const pulse = s.active ? 'animate-pulse' : '';
        const project = s.project.replace(/-home-jeremy-?/, '').replace(/workspace-/, '') || 'home';
        html += `
          <div class="p-4 bg-gray-800 rounded-lg border border-green-700 ${pulse}">
            <div class="flex items-center gap-3 mb-2">
              <span class="w-2 h-2 rounded-full ${s.active ? 'bg-green-400' : 'bg-yellow-400'}"></span>
              <span class="font-mono text-sm text-gray-400">${s.session_id.slice(0, 12)}</span>
              <span class="text-xs text-indigo-400">[${project}]</span>
              <span class="text-xs text-gray-500">${sizeStr}</span>
              <span class="text-xs text-gray-500 ml-auto">${ageStr}</span>
            </div>
            <div class="text-sm text-gray-300 truncate">${s.latest || '...'}</div>
          </div>`;
      }
      html += '</div>';
    }

    // Historical sessions
    const lines = (historical?.results || '').trim().split('\n').filter(l => l.trim());
    if (lines.length > 0) {
      html += '<h2 class="text-lg font-semibold mb-3 text-gray-400">Recent Sessions</h2>';
      html += '<div class="space-y-2">';
      for (const line of lines) {
        const match = line.trim().match(/^(\S+)\s+(\S+)\s+(\S+)\s+(.*?)(\[.*\])?$/);
        if (match) {
          const [, id, type, date, title, project] = match;
          html += `
            <div class="flex items-center gap-3 p-3 bg-gray-800 rounded-lg hover:bg-gray-750 cursor-pointer border border-gray-700"
                 onclick="navigateTo('/source/${id}')">
              <span class="font-mono text-xs text-gray-500">${id}</span>
              <span class="text-xs text-gray-400">${date}</span>
              <span class="flex-1 truncate">${title || ''}</span>
              <span class="text-xs text-indigo-400">${project || ''}</span>
            </div>`;
        }
      }
      html += '</div>';
    }

    content.innerHTML = html || '<div class="text-gray-400">No sessions found</div>';
  }

  await refresh();
  sessionsInterval = setInterval(refresh, 5000);  // Auto-refresh every 5s
}

async function renderSearch(query, project) {
  pageTitle.textContent = query ? `Search: ${query}${project ? ' [' + project + ']' : ''}` : 'Search';
  if (!query) {
    content.innerHTML = '<div class="text-gray-400">Enter a search query above</div>';
    return;
  }
  let url = `/api/search?q=${encodeURIComponent(query)}&or=1&limit=20`;
  if (project) url += `&project=${encodeURIComponent(project)}`;
  const data = await api(url);

  if (data.error) {
    content.innerHTML = `<div class="text-red-400">${data.error}</div>`;
    return;
  }

  const results = Array.isArray(data) ? data : [];
  if (results.length === 0) {
    content.innerHTML = '<div class="text-gray-400">No results found.</div>';
    return;
  }

  let html = `<div class="text-sm text-gray-500 mb-4">${results.length} results</div><div class="space-y-3">`;
  for (const r of results) {
    const type = r.result_type === 'thought' ? 'T' : 'D';
    const typeBg = r.result_type === 'thought' ? 'bg-blue-900' : 'bg-gray-700';
    const proj = r.project ? `<span class="text-xs text-indigo-400">[${r.project}]</span>` : '';
    const title = (r.source_title || '?').slice(0, 60);
    const srcId = (r.source_id || '').slice(0, 12);
    const turn = r.turn_number || '?';

    // Truncate content for preview, preserve first meaningful line
    let preview = (r.content || '').slice(0, 300);
    if (r.content && r.content.length > 300) preview += '…';

    html += `
      <div class="p-4 bg-gray-800 rounded-lg border border-gray-700 hover:border-indigo-500 cursor-pointer transition-colors"
           onclick="navigateTo('/source/${r.source_id}?turn=${r.turn_number}')">
        <div class="flex items-center gap-2 mb-2">
          <span class="px-1.5 py-0.5 ${typeBg} rounded text-xs font-mono">${type}</span>
          <span class="text-sm font-medium truncate">${title}</span>
          <span class="text-xs text-gray-500">t${turn}</span>
          ${proj}
          <span class="text-xs text-gray-600 ml-auto font-mono">src:${srcId}</span>
        </div>
        <div class="text-sm text-gray-400 leading-relaxed" id="preview-${srcId}">${preview.replace(/</g, '&lt;').replace(/\n/g, '<br>')}</div>
      </div>`;
  }
  html += '</div>';
  content.innerHTML = html;
}

async function renderSource(id, highlightTurn) {
  pageTitle.textContent = `Source: ${id.slice(0, 12)}`;
  const data = await api(`/api/source/${id}`);
  if (data.error) {
    content.innerHTML = `<div class="text-red-400">${data.error}</div>`;
    return;
  }

  const src = data.source || {};
  const entries = data.entries || [];
  const edges = data.edges || [];
  const srcType = src.type || 'unknown';

  // ── Header ──────────────────────────────────────
  const typeBadges = {
    note: 'bg-yellow-700', session: 'bg-green-700', conversation: 'bg-blue-700',
    status: 'bg-purple-700', docs: 'bg-teal-700', 'agent-run': 'bg-orange-700',
    musing: 'bg-pink-700', 'git-log': 'bg-gray-600', playbook: 'bg-indigo-700',
  };
  const badgeCls = typeBadges[srcType] || 'bg-gray-700';
  const proj = src.project ? `<span class="text-xs text-indigo-400">[${src.project}]</span>` : '';
  const date = (src.created_at || '').slice(0, 10);

  let html = `
    <div class="mb-6">
      <div class="flex items-center gap-3 mb-2">
        <span class="px-2 py-0.5 ${badgeCls} rounded text-xs font-semibold">${srcType}</span>
        ${proj}
        <span class="text-xs text-gray-500">${date}</span>
        <span class="text-xs text-gray-600 font-mono ml-auto">${src.id?.slice(0, 12) || ''}</span>
      </div>
      <h1 class="text-xl font-bold">${src.title || 'Untitled'}</h1>
    </div>`;

  // ── Type-aware content rendering ────────────────
  if (srcType === 'note') {
    // Notes: single card, no turn numbers
    const text = entries[0]?.content || '';
    html += `<div class="p-4 bg-gray-800 rounded-lg border-l-4 border-yellow-600">`;
    html += `<div class="markdown-body" id="note-content"></div>`;
    html += `</div>`;

  } else if (srcType === 'session' || srcType === 'conversation' || srcType === 'agent-run') {
    // Chat-style: user left, assistant right
    html += `<div class="space-y-4" id="chat-entries">`;
    for (const e of entries) {
      const isUser = e.entry_type === 'thought';
      const align = isUser ? 'mr-16' : 'ml-16';
      const border = isUser ? 'border-l-4 border-blue-500' : 'border-l-4 border-gray-600';
      const roleBadge = isUser
        ? '<span class="text-xs text-blue-400 font-semibold">USER</span>'
        : '<span class="text-xs text-gray-400 font-semibold">ASSISTANT</span>';
      const turnNum = e.turn_number || '?';
      const highlight = (highlightTurn && turnNum == highlightTurn) ? 'ring-2 ring-indigo-500' : '';

      html += `
        <div class="p-4 bg-gray-800 rounded-lg ${border} ${align} ${highlight}" id="turn-${turnNum}">
          <div class="flex items-center gap-2 mb-2">
            ${roleBadge}
            <span class="text-xs text-gray-600">t${turnNum}</span>
          </div>
          <div class="markdown-body entry-content text-sm" data-turn="${turnNum}"></div>
        </div>`;
    }
    html += `</div>`;

  } else {
    // Default: simple sequential rendering for docs, status, musings, etc.
    html += `<div class="space-y-4" id="doc-entries">`;
    for (const e of entries) {
      html += `<div class="markdown-body entry-content text-sm" data-turn="${e.turn_number || ''}"></div>`;
    }
    html += `</div>`;
  }

  // ── Edges sidebar (if any) ──────────────────────
  if (edges.length > 0) {
    html += `
      <div class="mt-8 p-4 bg-gray-800 rounded-lg border border-gray-700">
        <h3 class="text-sm font-semibold text-gray-400 mb-3">Connections (${edges.length})</h3>
        <div class="space-y-1">`;
    for (const e of edges.slice(0, 20)) {
      const rel = e.relation || '?';
      const other = e.source_id === src.id ? e.target_id : e.source_id;
      const otherType = e.source_id === src.id ? e.target_type : e.source_type;
      const meta = typeof e.metadata === 'string' ? JSON.parse(e.metadata || '{}') : (e.metadata || {});
      const turns = meta.turns ? ` t${meta.turns.from}${meta.turns.to !== meta.turns.from ? '-' + meta.turns.to : ''}` : '';
      const note = meta.note ? ` — ${meta.note.slice(0, 50)}` : '';
      html += `
          <div class="text-xs text-gray-400 hover:text-gray-200 cursor-pointer"
               onclick="navigateTo('/${otherType === 'source' ? 'source' : 'bead'}/${other}')">
            <span class="text-indigo-400">${rel}</span> → ${other.slice(0, 12)} [${otherType}]${turns}${note}
          </div>`;
    }
    html += `</div></div>`;
  }

  content.innerHTML = html;

  // ── Render markdown content ─────────────────────
  if (srcType === 'note') {
    const el = document.getElementById('note-content');
    if (el && entries[0]) el.appendChild(renderMd(entries[0].content));
  } else {
    const contentEls = content.querySelectorAll('.entry-content');
    entries.forEach((e, i) => {
      if (contentEls[i]) {
        contentEls[i].appendChild(renderMd(e.content));
      }
    });
  }

  // Scroll to highlighted turn
  if (highlightTurn) {
    const el = document.getElementById(`turn-${highlightTurn}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

// ── Terminal ─────────────────────────────────────────────────

let activeTerm = null;
let activeWs = null;

function destroyTerminal() {
  if (activeWs) { try { activeWs.close(); } catch(e) {} activeWs = null; }
  if (activeTerm) { activeTerm.dispose(); activeTerm = null; }
}

async function refreshTerminalPills() {
  const pillBar = document.getElementById('terminal-pills');
  if (!pillBar) return;
  const terminals = await api('/api/terminals');
  if (Array.isArray(terminals) && terminals.length > 0) {
    pillBar.innerHTML = '<span class="text-gray-600 self-center">|</span>' +
      terminals.map(t => {
        const cmd = (t.cmd || '').toLowerCase();
        const isClaude = cmd.includes('claude') || cmd.includes('autonomy-agent-claude');
        const isContainer = cmd.includes('docker') || cmd.includes('autonomy-agent');
        const icon = isClaude ? '🤖' : '⬛';
        const border = isContainer ? 'border border-purple-500' : 'border border-gray-600';
        const label = isClaude ? 'claude' : 'bash';
        return `
          <button onclick="reconnectTerminal('${t.id}')"
                  class="px-3 py-1 bg-gray-700 rounded text-sm hover:bg-gray-600 flex items-center gap-2 ${border}">
            <span class="w-2 h-2 rounded-full bg-green-400"></span>${icon} ${t.id} <span class="text-xs text-gray-500">${label}</span>
          </button>
          <button onclick="killTerminal('${t.id}')"
                  class="px-2 py-1 bg-red-900 rounded text-xs hover:bg-red-700">✕</button>`;
      }).join('');
  } else {
    pillBar.innerHTML = '';
  }
}

async function renderTerminal(cmd, attach) {
  pageTitle.textContent = attach ? `Attached: ${attach}` : 'Terminal';
  if (sessionsInterval) { clearInterval(sessionsInterval); sessionsInterval = null; }
  destroyTerminal();

  content.innerHTML = `
    <div class="flex gap-2 mb-3 flex-wrap">
      <button onclick="launchClaude()" class="px-3 py-1 bg-indigo-600 rounded text-sm hover:bg-indigo-500">Claude (host)</button>
      <button onclick="launchClaudeContainer()" class="px-3 py-1 bg-purple-600 rounded text-sm hover:bg-purple-500">Claude (container)</button>
      <button onclick="launchBash()" class="px-3 py-1 bg-gray-700 rounded text-sm hover:bg-gray-600">Bash</button>
      <button onclick="launchBashContainer()" class="px-3 py-1 bg-gray-600 rounded text-sm hover:bg-gray-500">Bash (container)</button>
      <span id="terminal-pills" class="contents"></span>
      <span class="text-xs text-gray-500 ml-auto self-center" id="term-status">ready</span>
    </div>
    <div id="terminal-container" style="height: calc(100vh - 10rem);"></div>`;

  // Show existing pills
  await refreshTerminalPills();

  // If we have an attach target, connect immediately
  if (!attach && !cmd) return;

  const termContainer = document.getElementById('terminal-container');
  const statusEl = document.getElementById('term-status');

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    theme: {
      background: '#111827',
      foreground: '#e5e7eb',
      cursor: '#818cf8',
      selectionBackground: '#4f46e580',
    },
  });

  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(termContainer);
  fitAddon.fit();

  // Build WebSocket URL
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let wsUrl = `${proto}//${location.host}/ws/terminal`;
  const params = new URLSearchParams();
  if (attach) params.set('attach', attach);
  else if (cmd) params.set('cmd', cmd);
  if (params.toString()) wsUrl += '?' + params.toString();

  const ws = new WebSocket(wsUrl);
  activeWs = ws;
  activeTerm = term;

  ws.onopen = async () => {
    statusEl.textContent = 'connected';
    statusEl.className = 'text-xs text-green-400 ml-auto self-center';
    // Send initial size
    const dims = fitAddon.proposeDimensions();
    if (dims) {
      ws.send(`\x1b[8;${dims.rows};${dims.cols}t`);
    }
    // Refresh pill bar now that tmux session exists
    await refreshTerminalPills();
  };

  ws.onmessage = (e) => {
    term.write(e.data);
  };

  ws.onclose = () => {
    statusEl.textContent = 'disconnected';
    statusEl.className = 'text-xs text-red-400 ml-auto self-center';
    term.write('\r\n\x1b[90m--- session ended ---\x1b[0m\r\n');
  };

  ws.onerror = () => {
    statusEl.textContent = 'error';
    statusEl.className = 'text-xs text-red-400 ml-auto self-center';
  };

  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(data);
    }
  });

  // ── Clipboard Integration ──────────────────────────
  // Two mechanisms:
  // 1. xterm.js selection → browser clipboard (for selecting visible text)
  // 2. OSC 52 from the PTY → browser clipboard (for tmux/vim yank)

  // Helper: copy text to clipboard with fallback for non-HTTPS
  function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).catch(() => {});
    } else {
      // Fallback: hidden textarea + execCommand
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
  }

  // 1. Selection → clipboard
  term.onSelectionChange(() => {
    const sel = term.getSelection();
    if (sel) {
      copyToClipboard(sel);
    }
  });

  // 2. OSC 52 handler: when the PTY sends \e]52;c;BASE64\a we decode and copy
  // This lets tmux `set -g set-clipboard on` work through the browser
  const origWrite = term.write.bind(term);
  const osc52Regex = /\x1b\]52;[a-z]*;([A-Za-z0-9+/=]*)\x07/g;
  term.write = function(data) {
    if (typeof data === 'string') {
      const matches = [...data.matchAll(osc52Regex)];
      for (const m of matches) {
        try {
          const decoded = atob(m[1]);
          copyToClipboard(decoded);
        } catch(e) {}
      }
    }
    return origWrite(data);
  };

  // Paste: right-click or Ctrl+Shift+V
  termContainer.addEventListener('contextmenu', async (e) => {
    e.preventDefault();
    try {
      const text = await navigator.clipboard.readText();
      if (text && ws.readyState === WebSocket.OPEN) {
        ws.send(text);
      }
    } catch(err) {}
  });

  // Ctrl+Shift+V paste
  termContainer.addEventListener('keydown', async (e) => {
    if (e.ctrlKey && e.shiftKey && e.key === 'V') {
      e.preventDefault();
      try {
        const text = await navigator.clipboard.readText();
        if (text && ws.readyState === WebSocket.OPEN) {
          ws.send(text);
        }
      } catch(err) {}
    }
  });

  // Handle resize
  const resizeObserver = new ResizeObserver(() => {
    fitAddon.fit();
    const dims = fitAddon.proposeDimensions();
    if (dims && ws.readyState === WebSocket.OPEN) {
      ws.send(`\x1b[8;${dims.rows};${dims.cols}t`);
    }
  });
  resizeObserver.observe(termContainer);
}

function launchClaude() {
  renderTerminal('claude --dangerously-skip-permissions');
}

function launchClaudeContainer() {
  renderTerminal('autonomy-agent-claude');
}

function launchBash() {
  renderTerminal('/bin/bash');
}

function launchBashContainer() {
  renderTerminal('autonomy-agent-bash');
}

function reconnectTerminal(name) {
  renderTerminal(null, name);
}

async function killTerminal(name) {
  await api(`/api/terminal/${name}/kill`);
  renderTerminal(null, null);  // Refresh the terminal page
}

// ── Router ───────────────────────────────────────────────────

function navigateTo(path) {
  history.pushState({}, '', path);
  route();
}

function route() {
  const path = window.location.pathname;

  // Clear any auto-refresh intervals from previous page
  if (sessionsInterval) { clearInterval(sessionsInterval); sessionsInterval = null; }

  // Update active nav
  document.querySelectorAll('.nav-link').forEach(el => {
    el.classList.toggle('active', path.startsWith('/' + el.dataset.page));
  });

  if (path === '/' || path === '/beads') {
    renderBeads();
  } else if (path.startsWith('/bead/')) {
    renderBeadDetail(path.split('/bead/')[1]);
  } else if (path === '/sessions') {
    renderSessions();
  } else if (path === '/search') {
    const params = new URLSearchParams(window.location.search);
    renderSearch(params.get('q'), params.get('project'));
  } else if (path.startsWith('/source/')) {
    const params = new URLSearchParams(window.location.search);
    renderSource(path.split('/source/')[1], params.get('turn'));
  } else if (path === '/terminal' || path.startsWith('/terminal/')) {
    const sessionId = path.startsWith('/terminal/') ? path.split('/terminal/')[1] : null;
    renderTerminal(null, sessionId);
  } else {
    content.innerHTML = '<div class="text-gray-400">Page not found</div>';
  }
}

// ── Event Handlers ───────────────────────────────────────────

// Global search — scope-aware
globalSearch.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    const q = globalSearch.value.trim();
    if (q) {
      const scope = document.getElementById('scope-select')?.value || '';
      let url = `/search?q=${encodeURIComponent(q)}`;
      if (scope) url += `&project=${encodeURIComponent(scope)}`;
      navigateTo(url);
    }
  }
});

// Client-side nav (no full page reload)
document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]');
  if (link && link.origin === window.location.origin) {
    e.preventDefault();
    navigateTo(link.pathname);
  }
});

window.addEventListener('popstate', route);

// ── Init ─────────────────────────────────────────────────────

// Load stats
api('/api/stats').then(data => {
  statsSummary.textContent = data.results || '';
});

// Load project list for scope picker
api('/api/projects').then(data => {
  const select = document.getElementById('scope-select');
  const lines = (data.results || '').split('\n');
  for (const line of lines) {
    const match = line.trim().match(/^\s*(\S+)\s+\d+/);
    if (match && !match[1].includes('─') && match[1] !== 'Project') {
      const opt = document.createElement('option');
      opt.value = match[1];
      opt.textContent = match[1];
      select.appendChild(opt);
    }
  }
});

// Initial route
route();
