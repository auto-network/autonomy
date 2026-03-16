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
  const el = document.createElement('div');
  el.appendChild(renderMd('```\n' + (data.results || 'No results') + '\n```'));
  content.innerHTML = '';
  content.appendChild(el);
}

async function renderSource(id) {
  pageTitle.textContent = `Source: ${id}`;
  const data = await api(`/api/source/${id}`);
  if (data.error) {
    content.innerHTML = `<div class="text-red-400">${data.error}</div>`;
    return;
  }
  content.innerHTML = '';
  content.appendChild(renderMd(data.content || 'No content'));
}

// ── Terminal ─────────────────────────────────────────────────

let activeTerm = null;
let activeWs = null;

function destroyTerminal() {
  if (activeWs) { try { activeWs.close(); } catch(e) {} activeWs = null; }
  if (activeTerm) { activeTerm.dispose(); activeTerm = null; }
}

function renderTerminal(cmd, attach) {
  pageTitle.textContent = attach ? `Attached: ${attach}` : 'Terminal';
  if (sessionsInterval) { clearInterval(sessionsInterval); sessionsInterval = null; }
  destroyTerminal();

  content.innerHTML = `
    <div class="flex gap-2 mb-3">
      <button onclick="launchClaude()" class="px-3 py-1 bg-indigo-600 rounded text-sm hover:bg-indigo-500">New Claude</button>
      <button onclick="launchBash()" class="px-3 py-1 bg-gray-700 rounded text-sm hover:bg-gray-600">New Bash</button>
      <span class="text-xs text-gray-500 ml-auto self-center" id="term-status">connecting...</span>
    </div>
    <div id="terminal-container" style="height: calc(100vh - 10rem);"></div>`;

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

  ws.onopen = () => {
    statusEl.textContent = 'connected';
    statusEl.className = 'text-xs text-green-400 ml-auto self-center';
    // Send initial size
    const dims = fitAddon.proposeDimensions();
    if (dims) {
      ws.send(`\x1b[8;${dims.rows};${dims.cols}t`);
    }
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

function launchBash() {
  renderTerminal('/bin/bash');
}

// ── Router ───────────────────────────────────────────────────

function navigateTo(path) {
  history.pushState({}, '', path);
  route();
}

function route() {
  const path = window.location.pathname;

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
    renderSource(path.split('/source/')[1]);
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
