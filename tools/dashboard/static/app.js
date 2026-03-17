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

// ── Bead Actions ────────────────────────────────────────────

async function approveBead(id, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const btn = document.getElementById(`approve-btn-${id}`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '...';
  }
  const res = await fetch(`/api/bead/${id}/approve`, { method: 'POST' });
  const data = await res.json();
  if (data.ok) {
    if (btn) {
      // Replace button with "Approved" badge inline
      const badge = document.createElement('span');
      badge.className = 'px-2 py-0.5 bg-green-900 text-green-300 text-xs rounded font-semibold';
      badge.textContent = 'Approved';
      btn.replaceWith(badge);
    } else {
      navigateTo(`/bead/${id}`);
    }
  } else {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Approve';
    }
    alert(`Failed to approve: ${data.error}`);
  }
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
    const labels = issue.labels || [];
    const isApproved = labels.includes('readiness:approved');
    const canApprove = issue.status !== 'closed' && !isApproved;
    const approveHtml = isApproved
      ? `<span class="px-2 py-0.5 bg-green-900 text-green-300 text-xs rounded font-semibold">Approved</span>`
      : canApprove
      ? `<button id="approve-btn-${issue.id}" onclick="event.stopPropagation(); approveBead('${issue.id}', event)"
                 class="px-2 py-0.5 bg-green-700 hover:bg-green-600 text-white text-xs rounded">Approve</button>`
      : '';
    return `
      <div class="p-4 sm:p-3 bg-gray-800 rounded-lg hover:bg-gray-750 cursor-pointer border border-gray-700"
           onclick="navigateTo('/bead/${issue.id}')">
        <div class="flex items-center gap-2 mb-1 sm:mb-0">
          <span>${type}</span>
          <span class="truncate text-sm sm:text-base">${issue.title}</span>
        </div>
        <div class="flex items-center gap-2 flex-wrap mt-1 sm:mt-0">
          <span class="font-mono text-xs text-gray-500">${issue.id}</span>
          ${priorityBadge(issue.priority)}
          ${statusBadge(issue.status)}
          ${approveHtml}
        </div>
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

  const labels = bead.labels || [];
  const isApproved = labels.includes('readiness:approved');
  const isClosed = bead.status === 'closed';
  const approveBtn = (!isApproved && !isClosed)
    ? `<button id="approve-btn-${bead.id}" onclick="approveBead('${bead.id}', event)" class="px-3 py-1 bg-green-700 hover:bg-green-600 text-white text-sm rounded">Approve for Dispatch</button>`
    : isApproved
    ? `<span class="px-3 py-1 bg-green-900 text-green-300 text-sm rounded">Approved</span>`
    : '';

  let html = `
    <div class="mb-6">
      <h1 class="text-2xl font-bold mb-2">${bead.title}</h1>
      <div class="flex gap-2 items-center mb-4">
        <span class="font-mono text-sm text-gray-500">${bead.id}</span>
        ${priorityBadge(bead.priority)}
        ${statusBadge(bead.status)}
        <span class="text-sm text-gray-400">${bead.issue_type}</span>
        ${approveBtn}
      </div>
      ${labels.length ? `<div class="flex gap-1 mb-2">${labels.map(l => `<span class="px-2 py-0.5 bg-gray-700 text-gray-300 text-xs rounded">${l}</span>`).join('')}</div>` : ''}
    </div>`;

  if (primer.content) {
    html += '<div class="mt-6" id="primer-content"></div>';
  }

  // Add padding at bottom if live panel might open
  const isRunning = labels.some(l => l.startsWith('dispatch:running') || l.startsWith('dispatch:launching') || l.startsWith('dispatch:collecting'));
  if (isRunning) {
    html += '<div style="height: 20rem;"></div>';  // spacer for live panel
  }

  content.innerHTML = html;

  // Render primer as markdown
  if (primer.content) {
    document.getElementById('primer-content').appendChild(renderMd(primer.content));
  }

  // Auto-open live panel if dispatch is running
  if (isRunning) {
    const runs = await api('/api/dispatch/runs');
    const runsList = Array.isArray(runs) ? runs : [];
    const beadRun = runsList.find(r => r.bead_id === id);
    if (beadRun) {
      showLivePanel(beadRun.dir);
    }
  }
}

let dispatchInterval = null;

function _formatTokenCount(bytes) {
  const tokens = Math.round(bytes / 4);
  if (tokens >= 1000000) return (tokens / 1000000).toFixed(1) + 'M tok';
  if (tokens >= 1000) return (tokens / 1000).toFixed(0) + 'k tok';
  return tokens + ' tok';
}

async function _loadDispatchSnippets(activeBeads, runsByBead) {
  const fetches = activeBeads.map(async (b) => {
    const run = runsByBead[b.id];
    if (!run) return;
    const el = document.getElementById(`snippet-${b.id}`);
    if (!el) return;
    const snippet = await getLiveSnippet(run.dir);
    const textEl = el.querySelector('.snippet-text');
    const tokensEl = el.querySelector('.snippet-tokens');
    if (snippet) {
      if (textEl && snippet.text) textEl.textContent = snippet.text;
      if (tokensEl && snippet.file_size_bytes > 0) {
        tokensEl.textContent = '~' + _formatTokenCount(snippet.file_size_bytes);
      }
    }
  });
  await Promise.all(fetches);
}

async function renderDispatch() {
  pageTitle.textContent = 'Dispatch';
  if (dispatchInterval) clearInterval(dispatchInterval);

  async function refresh() {
    const status = await api('/api/dispatch/status');
    const allBeads = await api('/api/beads/list');
    const beadList = Array.isArray(allBeads) ? allBeads : [];

    // Build container lookup by bead ID (extract from container name: agent-<bead-id>-<pid>)
    const containersByBead = {};
    for (const c of (status.containers || [])) {
      if (c.name.startsWith('agent-slack')) continue;
      const parts = c.name.replace('agent-', '').split('-');
      parts.pop();
      const beadId = parts.join('-');
      containersByBead[beadId] = c;
    }

    // Helper: extract dispatch state from labels
    function getDispatchState(bead) {
      for (const l of (bead.labels || [])) {
        if (l.startsWith('dispatch:')) return l.split(':')[1];
      }
      return null;
    }

    // Active: beads with active dispatch states (or in_progress for backward compat)
    const activeDispatchStates = new Set(['queued', 'launching', 'running', 'collecting', 'merging']);
    const active = beadList.filter(b => {
      const ds = getDispatchState(b);
      return (ds && activeDispatchStates.has(ds)) || b.status === 'in_progress';
    });

    // Approved: open beads with readiness:approved, waiting for dispatch
    const approvedBeads = beadList
      .filter(b => b.status === 'open' && (b.labels || []).includes('readiness:approved')
              && !active.some(a => a.id === b.id));

    let html = '';
    let runsByBead = {};

    // Active dispatches — bead + container unified
    html += `<div class="mb-8">
      <h2 class="text-lg font-semibold mb-3 text-green-400">Active Dispatches</h2>`;
    if (active.length > 0) {
      // Fetch latest runs to match active beads to run dirs
      const runsData = await api('/api/dispatch/runs');
      const runsList = Array.isArray(runsData) ? runsData : [];
      for (const r of runsList) {
        if (!runsByBead[r.bead_id]) runsByBead[r.bead_id] = r;
      }

      for (const b of active) {
        const container = containersByBead[b.id];
        const ds = getDispatchState(b);
        const stateColors = { queued: 'blue', launching: 'yellow', running: 'green', collecting: 'purple', merging: 'indigo' };
        const stateColor = stateColors[ds] || 'gray';
        const stateBadge = ds
          ? `<span class="px-2 py-0.5 bg-${stateColor}-900 text-${stateColor}-300 text-xs rounded font-mono">${ds}</span>`
          : '';
        const containerHtml = container
          ? `<div class="mt-2 ml-6 flex items-center gap-2">
               <span class="w-2 h-2 bg-green-400 rounded-full animate-pulse"></span>
               <span class="font-mono text-xs text-gray-400">${container.name}</span>
               <span class="text-xs text-gray-500">${container.status}</span>
               <span class="text-xs text-gray-600">${container.image}</span>
             </div>`
          : ds === 'collecting' || ds === 'merging'
          ? `<div class="mt-2 ml-6 text-xs text-gray-500">Agent finished — ${ds}...</div>`
          : `<div class="mt-2 ml-6 text-xs text-gray-500">Container exited — waiting for results</div>`;

        // Live view button + snippet area
        const run = runsByBead[b.id];
        const runDir = run ? run.dir : '';
        const liveBtn = runDir
          ? `<button onclick="event.preventDefault(); event.stopPropagation(); showLivePanel('${runDir}')"
                    class="px-2 py-0.5 bg-indigo-700 hover:bg-indigo-600 text-white text-xs rounded ml-2">View Live</button>`
          : '';
        const snippetId = `snippet-${b.id}`;

        html += `
          <a href="/bead/${b.id}" class="block p-4 sm:p-3 bg-gray-800 rounded-lg mb-2 border-l-4 border-green-500 hover:bg-gray-750">
            <div class="mb-1 sm:mb-0">
              <div class="truncate text-sm sm:text-base">${b.title}</div>
              <div class="flex gap-2 items-center flex-wrap mt-1">
                <span class="font-mono text-xs text-gray-400">${b.id}</span>
                ${priorityBadge(b.priority)}
                ${stateBadge}
                ${liveBtn}
              </div>
            </div>
            ${containerHtml}
            <div id="${snippetId}" class="mt-2 ml-6 flex items-center gap-2" data-run="${runDir}">
              <span class="snippet-text text-xs text-gray-500 italic truncate flex-1"></span>
              <span class="snippet-tokens text-xs text-gray-600 font-mono whitespace-nowrap"></span>
            </div>
          </a>`;
      }
    } else {
      html += `<div class="text-gray-500 text-sm">No active dispatches</div>`;
    }
    html += `</div>`;

    // Approved & waiting
    html += `<div class="mb-8">
      <h2 class="text-lg font-semibold mb-3 text-blue-400">Approved — Waiting for Dispatch</h2>`;
    if (approvedBeads.length > 0) {
      for (const b of approvedBeads) {
        html += `
          <a href="/bead/${b.id}" class="block p-4 sm:p-3 bg-gray-800 rounded-lg mb-2 border-l-4 border-blue-500 hover:bg-gray-750">
            <div class="truncate text-sm sm:text-base">${b.title}</div>
            <div class="flex gap-2 items-center flex-wrap mt-1">
              <span class="font-mono text-xs text-gray-400">${b.id}</span>
              ${priorityBadge(b.priority)}
            </div>
            <div class="flex gap-1 mt-1">${(b.labels||[]).map(l => `<span class="px-2 py-0.5 bg-gray-700 text-gray-300 text-xs rounded">${l}</span>`).join('')}</div>
          </a>`;
      }
    } else {
      html += `<div class="text-gray-500 text-sm">No beads approved for dispatch</div>`;
    }
    html += `</div>`;

    // Last Runs
    const runs = await api('/api/dispatch/runs');
    const completedRuns = Array.isArray(runs) ? runs.filter(r => r.decision) : [];
    html += `<div class="mb-8">
      <h2 class="text-lg font-semibold mb-3 text-gray-400">Last Runs</h2>`;
    if (completedRuns.length > 0) {
      for (const r of completedRuns) {
        const status = r.decision?.status || '?';
        const reason = r.decision?.reason || '';
        const statusColor = status === 'DONE' ? 'green' : status === 'BLOCKED' ? 'yellow' : 'red';
        const commitBadge = r.commit_hash
          ? `<span class="font-mono text-xs text-gray-400">${r.commit_hash.slice(0, 10)}</span>`
          : `<span class="text-xs text-gray-500">no commits</span>`;
        const ts = r.timestamp.replace('-', ' ');
        html += `
          <a href="/dispatch/trace/${r.dir}" class="block p-3 bg-gray-800 rounded-lg mb-2 border-l-4 border-${statusColor}-500 hover:bg-gray-750">
            <div class="flex justify-between items-center">
              <div class="flex gap-2 items-center">
                <span class="font-mono text-sm text-gray-400">${r.bead_id}</span>
                <span class="badge badge-${statusColor === 'green' ? 'closed' : statusColor === 'yellow' ? 'blocked' : 'open'}">${status}</span>
                ${commitBadge}
                <button onclick="event.preventDefault(); event.stopPropagation(); showCompletedPanel('${r.dir}')"
                        class="px-2 py-0.5 bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs rounded">Session</button>
              </div>
              <span class="text-xs text-gray-500">${ts}</span>
            </div>
            <div class="text-sm text-gray-300 mt-1 truncate">${reason}</div>
          </a>`;
      }
    } else {
      html += `<div class="text-gray-500 text-sm">No completed runs</div>`;
    }
    html += `</div>`;

    content.innerHTML = html;

    // Load snippets + token counts for active dispatches (after DOM is ready)
    _loadDispatchSnippets(active, runsByBead);
  }

  await refresh();
  dispatchInterval = setInterval(refresh, 5000); // auto-refresh every 5s
}

async function renderTrace(runName) {
  pageTitle.textContent = `Trace: ${runName}`;
  const trace = await api(`/api/dispatch/trace/${runName}`);
  if (trace.error) {
    content.innerHTML = `<div class="text-red-400">${trace.error}</div>`;
    return;
  }

  const bead = Array.isArray(trace.bead) ? trace.bead[0] : trace.bead;
  const decision = trace.decision || {};
  const status = decision.status || '?';
  const statusColor = status === 'DONE' ? 'green' : status === 'BLOCKED' ? 'yellow' : 'red';

  let html = `
    <div class="mb-6">
      <a href="/dispatch" class="text-indigo-400 text-sm hover:underline mb-2 block">← Back to Dispatch</a>
      <h1 class="text-2xl font-bold mb-2">${bead?.title || trace.bead_id}</h1>
      <div class="flex gap-2 items-center mb-4">
        <a href="/bead/${trace.bead_id}" class="font-mono text-sm text-indigo-400 hover:underline">${trace.bead_id}</a>
        ${bead ? priorityBadge(bead.priority) : ''}
        <span class="badge badge-${statusColor === 'green' ? 'closed' : 'open'}">${status}</span>
        ${trace.commit_hash ? `<span class="font-mono text-xs text-gray-400">${trace.commit_hash.slice(0, 10)}</span>` : ''}
      </div>
    </div>`;

  // Decision
  html += `<div class="mb-6">
    <h2 class="text-lg font-semibold mb-2 text-${statusColor}-400">Decision</h2>
    <div class="bg-gray-800 rounded-lg p-4">
      <div class="text-sm mb-2"><strong>Status:</strong> ${status}</div>
      <div class="text-sm mb-2"><strong>Reason:</strong> ${decision.reason || 'none'}</div>
      ${decision.notes ? `<div class="text-sm mb-2"><strong>Notes:</strong> ${decision.notes}</div>` : ''}
      ${decision.artifacts?.length ? `<div class="text-sm"><strong>Artifacts:</strong> ${decision.artifacts.join(', ')}</div>` : ''}
    </div>
  </div>`;

  // Discovered beads
  if (decision.discovered_beads?.length) {
    html += `<div class="mb-6">
      <h2 class="text-lg font-semibold mb-2 text-blue-400">Discovered Work</h2>`;
    for (const b of decision.discovered_beads) {
      html += `<div class="bg-gray-800 rounded-lg p-3 mb-2">
        <div class="font-semibold text-sm">${b.title}</div>
        <div class="text-xs text-gray-400 mt-1">${b.description?.slice(0, 200) || ''}</div>
      </div>`;
    }
    html += `</div>`;
  }

  // Git diff
  if (trace.diff) {
    html += `<div class="mb-6">
      <h2 class="text-lg font-semibold mb-2 text-purple-400">Diff</h2>
      <pre class="bg-gray-800 rounded-lg p-4 text-xs overflow-x-auto max-h-96 overflow-y-auto"><code>${escapeHtml(trace.diff)}</code></pre>
    </div>`;
  }

  // Experience report
  if (trace.experience_report) {
    html += `<div class="mb-6">
      <h2 class="text-lg font-semibold mb-2 text-yellow-400">Experience Report</h2>
      <div id="experience-content"></div>
    </div>`;
  }

  // Session log — opens in bottom-docked panel
  if (trace.has_session) {
    html += `<div class="mb-6">
      <button onclick="showCompletedPanel('${runName}')"
              class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm text-gray-300">
        View Session Log
      </button>
    </div>`;
  }

  content.innerHTML = html;

  if (trace.experience_report) {
    document.getElementById('experience-content').appendChild(renderMd(trace.experience_report));
  }

  // Session log now opens in the bottom-docked panel via showCompletedPanel()
}

async function loadSessionLog(runName) {
  const container = document.getElementById('session-log-container');
  const loadBtn = document.getElementById('session-log-load');
  loadBtn.textContent = 'Loading...';
  loadBtn.disabled = true;

  try {
    const data = await api(`/api/dispatch/tail/${runName}?after=0`);
    container.innerHTML = '';

    if (!data.entries || data.entries.length === 0) {
      container.innerHTML = '<div class="text-sm text-gray-500">No session entries found.</div>';
      return;
    }

    // Entry count summary
    const summary = document.createElement('div');
    summary.className = 'text-xs text-gray-500 mb-3';
    summary.textContent = `${data.entries.length} entries`;
    container.appendChild(summary);

    // Scrollable entries container
    const entriesEl = document.createElement('div');
    entriesEl.className = 'bg-gray-800 rounded-lg p-4 max-h-[80vh] overflow-y-auto';
    container.appendChild(entriesEl);

    _renderSessionEntries(data.entries, entriesEl);
  } catch (err) {
    container.innerHTML = `<div class="text-sm text-red-400">Failed to load session log: ${escapeHtml(err.message)}</div>`;
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
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
    const isUser = r.result_type === 'thought';
    const typeBadge = isUser
      ? '<span class="px-1.5 py-0.5 bg-blue-900 rounded text-xs">YOU</span>'
      : '<span class="px-1.5 py-0.5 bg-gray-700 rounded text-xs">AI</span>';
    const proj = r.project ? `<span class="text-xs text-indigo-400">[${r.project}]</span>` : '';
    const srcId = (r.source_id || '').slice(0, 12);
    const turn = r.turn_number || '?';

    // First line of content as the headline
    const lines = (r.content || '').split('\n').filter(l => l.trim());
    const headline = (lines[0] || '').slice(0, 120);
    // Rest as dimmer preview
    const rest = lines.slice(1).join('\n').slice(0, 200);
    const preview = rest ? rest.replace(/</g, '&lt;').replace(/\n/g, '<br>') + (r.content.length > 300 ? '…' : '') : '';

    // Source title — shortened, as secondary context
    const srcTitle = (r.source_title || '').slice(0, 40);
    const srcLabel = srcTitle.startsWith('Read all the markdown') ? 'main session' : srcTitle;

    html += `
      <div class="p-4 bg-gray-800 rounded-lg border border-gray-700 hover:border-indigo-500 cursor-pointer transition-colors"
           onclick="navigateTo('/source/${r.source_id}?turn=${r.turn_number}')">
        <div class="text-sm font-medium mb-1">${headline.replace(/</g, '&lt;')}</div>
        ${preview ? `<div class="text-xs text-gray-500 leading-relaxed mb-2">${preview}</div>` : ''}
        <div class="flex items-center gap-2">
          ${typeBadge}
          <span class="text-xs text-gray-600">${srcLabel}</span>
          <span class="text-xs text-gray-600">t${turn}</span>
          ${proj}
          <span class="text-xs text-gray-700 font-mono ml-auto">src:${srcId}</span>
        </div>
      </div>`;
  }
  html += '</div>';
  content.innerHTML = html;
}

async function renderContext(id, turn, window = 5) {
  // Load just the context around a specific turn — not the whole source
  const srcData = await api(`/api/source/${id}`);
  const src = srcData.source || {};
  const allEntries = srcData.entries || [];
  const edges = srcData.edges || [];

  // Filter to window
  const entries = allEntries.filter(e => {
    const t = e.turn_number;
    return t && Math.abs(t - turn) <= window;
  });

  const totalTurns = allEntries.length;
  pageTitle.textContent = `${src.title?.slice(0, 40) || id.slice(0, 12)} — turn ${turn}`;

  const typeBadges = {
    note: 'bg-yellow-700', session: 'bg-green-700', conversation: 'bg-blue-700',
    status: 'bg-purple-700', docs: 'bg-teal-700', 'agent-run': 'bg-orange-700',
  };
  const badgeCls = typeBadges[src.type] || 'bg-gray-700';
  const proj = src.project ? `<span class="text-xs text-indigo-400">[${src.project}]</span>` : '';
  const date = (src.created_at || '').slice(0, 10);

  let html = `
    <div class="mb-4">
      <div class="flex items-center gap-3 mb-2">
        <span class="px-2 py-0.5 ${badgeCls} rounded text-xs font-semibold">${src.type || '?'}</span>
        ${proj}
        <span class="text-xs text-gray-500">${date}</span>
        <span class="text-xs text-gray-600 font-mono ml-auto">${src.id?.slice(0, 12) || ''}</span>
      </div>
      <h1 class="text-lg font-bold">${src.title || 'Untitled'}</h1>
      <div class="text-xs text-gray-500 mt-1">
        Showing turns ${turn - window}–${turn + window} of ${totalTurns}
        <a href="/source/${id}" class="text-indigo-400 ml-2 hover:underline">View full source →</a>
      </div>
    </div>`;

  // Render entries in chat style
  html += `<div class="space-y-3">`;
  for (const e of entries) {
    const isUser = e.entry_type === 'thought';
    const align = isUser ? 'ml-16' : 'mr-16';
    const border = isUser ? 'border-r-4 border-blue-500' : 'border-l-4 border-gray-600';
    const roleBadge = isUser
      ? '<span class="text-xs text-blue-400 font-semibold">YOU</span>'
      : '<span class="text-xs text-gray-400 font-semibold">ASSISTANT</span>';
    const t = e.turn_number || '?';
    const highlight = (t == turn) ? 'ring-2 ring-indigo-500 bg-gray-750' : '';

    html += `
      <div class="p-3 bg-gray-800 rounded-lg ${border} ${align} ${highlight}" id="turn-${t}">
        <div class="flex items-center gap-2 mb-1">
          ${roleBadge}
          <span class="text-xs text-gray-600">t${t}</span>
        </div>
        <div class="markdown-body entry-content text-sm" data-turn="${t}"></div>
      </div>`;
  }
  html += `</div>`;

  // Navigation: load more context
  html += `
    <div class="flex gap-4 mt-4 justify-center">
      <button onclick="renderContext('${id}', ${turn}, ${window + 5})"
              class="px-3 py-1 bg-gray-700 rounded text-sm hover:bg-gray-600">Show more context</button>
      <a href="/source/${id}" class="px-3 py-1 bg-gray-700 rounded text-sm hover:bg-gray-600 inline-block">Full source</a>
    </div>`;

  content.innerHTML = html;

  // Render markdown
  const contentEls = content.querySelectorAll('.entry-content');
  entries.forEach((e, i) => {
    if (contentEls[i]) contentEls[i].appendChild(renderMd(e.content));
  });
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
let activeTerminalId = null;
let _pillClickTimer = null;

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
        const isContainer = t.env === 'container' || cmd.includes('docker') || cmd.includes('autonomy-agent');
        const icon = isClaude ? '🤖' : '⬛';
        const border = isContainer ? 'border border-purple-500' : 'border border-gray-600';
        const label = isClaude ? 'claude' : 'bash';
        const active = t.id === activeTerminalId;
        const ring = active ? 'ring-2 ring-indigo-500' : '';
        const bgClass = active ? 'bg-indigo-900' : 'bg-gray-700';
        const displayName = escapeHtml(t.name || t.id);
        const envBadge = isContainer
          ? '<span class="text-xs text-purple-400 font-medium">container</span>'
          : '<span class="text-xs text-gray-500">host</span>';
        return `
          <div class="flex items-center ${border} rounded overflow-hidden ${ring}">
            <div onclick="pillSingleClick('${t.id}')"
                 class="px-3 py-1 ${bgClass} text-sm hover:bg-gray-600 flex items-center gap-2 cursor-pointer">
              <span class="w-2 h-2 rounded-full bg-green-400"></span>
              ${icon}
              <span class="pill-name" ondblclick="startRenameTerminal(event, '${t.id}')">${displayName}</span>
              <span class="text-xs text-gray-500">${label}</span>
              <span class="text-xs text-gray-600">&middot;</span>
              ${envBadge}
            </div>
            <button onclick="killTerminal('${t.id}')"
                    class="px-2 py-1 bg-red-900 text-xs hover:bg-red-700 self-stretch">✕</button>
          </div>`;
      }).join('');
  } else {
    pillBar.innerHTML = '';
  }
}

function pillSingleClick(id) {
  clearTimeout(_pillClickTimer);
  _pillClickTimer = setTimeout(() => {
    _pillClickTimer = null;
    reconnectTerminal(id);
  }, 250);
}

function startRenameTerminal(event, id) {
  clearTimeout(_pillClickTimer);
  _pillClickTimer = null;
  event.stopPropagation();
  event.preventDefault();
  const span = event.target.closest('.pill-name');
  if (!span) return;
  const currentName = span.textContent;
  const input = document.createElement('input');
  input.type = 'text';
  input.value = currentName;
  input.className = 'bg-gray-800 text-white text-sm px-1 w-24 rounded outline-none border border-indigo-500';
  input.style.minWidth = '3rem';
  input.addEventListener('click', (e) => e.stopPropagation());
  const finish = async (save) => {
    if (save) {
      const newName = input.value.trim();
      if (newName && newName !== currentName) {
        await fetch(`/api/terminal/${id}/rename`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: newName}),
        });
      }
    }
    refreshTerminalPills();
  };
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  span.replaceWith(input);
  setTimeout(() => { input.focus(); input.select(); }, 0);
}

async function renderTerminal(cmd, attach) {
  // Auto-reconnect to previously active session when navigating back
  if (!cmd && !attach && activeTerminalId) {
    attach = activeTerminalId;
  }
  if (attach) activeTerminalId = attach;
  else if (cmd) activeTerminalId = null;
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
    // For new sessions, detect the newly created terminal
    if (!activeTerminalId) {
      const terms = await api('/api/terminals');
      if (Array.isArray(terms) && terms.length > 0) {
        const newest = terms.reduce((a, b) => (b.started || 0) > (a.started || 0) ? b : a);
        activeTerminalId = newest.id;
      }
    }
    // Refresh pill bar now that tmux session exists
    await refreshTerminalPills();
    term.focus();
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

  // ── Paste Handling ──────────────────────────────────────
  // Multiple paths to ensure paste works regardless of tmux mouse state:
  // 1. xterm.js onPaste (browser paste event — Ctrl+V, middle-click)
  // 2. Right-click context menu intercept
  // 3. Ctrl+Shift+V keyboard shortcut
  //
  // Uses bracket paste mode (\e[200~ ... \e[201~) to prevent shells from
  // executing multi-line pastes immediately.

  function bracketPaste(text) {
    if (text.includes('\n') || text.includes('\r')) {
      return `\x1b[200~${text}\x1b[201~`;
    }
    return text;
  }

  async function pasteFromClipboard() {
    try {
      const text = await navigator.clipboard.readText();
      if (text && ws.readyState === WebSocket.OPEN) {
        ws.send(bracketPaste(text));
      }
    } catch(err) {
      // Clipboard API may fail without HTTPS or user gesture — no fallback for read
      console.warn('Clipboard read failed:', err.message);
    }
  }

  // 1. xterm.js built-in paste: fires when browser delivers a paste event
  //    to the terminal canvas (Ctrl+V, middle-click, OS paste).
  //    We intercept here to send via WebSocket instead of xterm's default.
  term.onPaste((text) => {
    if (text && ws.readyState === WebSocket.OPEN) {
      ws.send(bracketPaste(text));
    }
  });

  // 2. Right-click → paste from clipboard (suppresses browser/tmux context menu)
  termContainer.addEventListener('contextmenu', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    await pasteFromClipboard();
  });

  // 3. Ctrl+Shift+V paste shortcut
  term.attachCustomKeyEventHandler((e) => {
    if (e.type === 'keydown' && e.ctrlKey && e.shiftKey && e.key === 'V') {
      e.preventDefault();
      pasteFromClipboard();
      return false;  // prevent xterm.js from processing this key
    }
    return true;
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
  // Skip if already connected to this session
  if (name === activeTerminalId && activeWs?.readyState === WebSocket.OPEN) {
    return;
  }
  activeTerminalId = name;
  renderTerminal(null, name);
}

async function killTerminal(name) {
  await api(`/api/terminal/${name}/kill`);
  if (activeTerminalId === name) activeTerminalId = null;
  renderTerminal(null, null);
}

// ── Live Session Panel ───────────────────────────────────────

let _livePanelInterval = null;
let _livePanelRunDir = null;
let _livePanelOffset = 0;
let _livePanelAutoScroll = true;

function showLivePanel(runDir) {
  const panel = document.getElementById('live-panel');
  const entries = document.getElementById('live-panel-entries');
  const beadLabel = document.getElementById('live-panel-bead');
  const statusEl = document.getElementById('live-panel-status');
  const pulseEl = document.getElementById('live-pulse');
  const badgeEl = document.getElementById('live-panel-badge');

  // Reset state
  _livePanelRunDir = runDir;
  _livePanelOffset = 0;
  _livePanelAutoScroll = true;
  entries.innerHTML = '';
  document.getElementById('live-resume-btn').style.display = 'none';

  // Extract bead ID from run dir name (format: <bead>-YYYYMMDD-HHMMSS)
  const parts = runDir.split('-');
  const beadId = parts.length >= 3 ? parts.slice(0, -2).join('-') : runDir;
  beadLabel.textContent = beadId;
  statusEl.textContent = 'connecting...';

  // Live appearance
  badgeEl.textContent = 'Live';
  badgeEl.className = 'badge badge-open';
  pulseEl.style.animation = '';
  pulseEl.style.background = '#22c55e';

  // Show panel and add padding to main content
  panel.style.display = 'flex';
  panel.classList.remove('collapsed');
  document.getElementById('content').style.paddingBottom = '20rem';

  // Set up scroll detection
  const body = document.getElementById('live-panel-body');
  body.onscroll = () => {
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 50;
    _livePanelAutoScroll = atBottom;
    document.getElementById('live-resume-btn').style.display = atBottom ? 'none' : 'block';
  };

  // Start polling
  if (_livePanelInterval) clearInterval(_livePanelInterval);
  _livePollTail(); // immediate first poll
  _livePanelInterval = setInterval(_livePollTail, 1500);
}

/**
 * Open the bottom-docked session panel for a completed dispatch.
 * Same panel as showLivePanel but loads all entries at once (no polling)
 * and shows a gray "Complete" badge instead of green "Live".
 */
async function showCompletedPanel(runDir) {
  const panel = document.getElementById('live-panel');
  const entries = document.getElementById('live-panel-entries');
  const beadLabel = document.getElementById('live-panel-bead');
  const statusEl = document.getElementById('live-panel-status');
  const pulseEl = document.getElementById('live-pulse');
  const badgeEl = document.getElementById('live-panel-badge');

  // Stop any existing polling
  if (_livePanelInterval) {
    clearInterval(_livePanelInterval);
    _livePanelInterval = null;
  }

  // Reset state
  _livePanelRunDir = runDir;
  _livePanelOffset = 0;
  _livePanelAutoScroll = true;
  entries.innerHTML = '';
  document.getElementById('live-resume-btn').style.display = 'none';

  // Extract bead ID
  const parts = runDir.split('-');
  const beadId = parts.length >= 3 ? parts.slice(0, -2).join('-') : runDir;
  beadLabel.textContent = beadId;

  // Completed appearance — gray badge, no pulse animation
  badgeEl.textContent = 'Complete';
  badgeEl.className = 'badge badge-closed';
  pulseEl.style.background = '#6b7280';
  pulseEl.style.animation = 'none';
  statusEl.textContent = 'loading...';
  statusEl.className = 'text-xs text-gray-500 ml-auto';

  // Show panel
  panel.style.display = 'flex';
  panel.classList.remove('collapsed');
  document.getElementById('content').style.paddingBottom = '20rem';

  // Scroll detection
  const body = document.getElementById('live-panel-body');
  body.onscroll = () => {
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 50;
    _livePanelAutoScroll = atBottom;
    document.getElementById('live-resume-btn').style.display = atBottom ? 'none' : 'block';
  };

  // Load all entries at once — no polling
  try {
    const data = await api(`/api/dispatch/tail/${runDir}?after=0`);
    if (data.entries && data.entries.length > 0) {
      _liveAppendEntries(data.entries);
      statusEl.textContent = `${data.entries.length} entries`;
    } else {
      entries.innerHTML = '<div class="text-sm text-gray-500 p-2">No session entries found.</div>';
      statusEl.textContent = 'empty';
    }
    if (data.offset !== undefined) {
      _livePanelOffset = data.offset;
    }
  } catch (err) {
    statusEl.textContent = 'error';
    statusEl.className = 'text-xs text-red-400 ml-auto';
  }
}

function hideLivePanel() {
  const panel = document.getElementById('live-panel');
  panel.style.display = 'none';
  panel.classList.add('collapsed');
  document.getElementById('content').style.paddingBottom = '';
  if (_livePanelInterval) {
    clearInterval(_livePanelInterval);
    _livePanelInterval = null;
  }
  _livePanelRunDir = null;
}

function toggleLivePanel() {
  const panel = document.getElementById('live-panel');
  panel.classList.toggle('collapsed');
}

function liveResumeScroll() {
  _livePanelAutoScroll = true;
  document.getElementById('live-resume-btn').style.display = 'none';
  const body = document.getElementById('live-panel-body');
  body.scrollTop = body.scrollHeight;
}

async function _livePollTail() {
  if (!_livePanelRunDir) return;

  try {
    const data = await api(`/api/dispatch/tail/${_livePanelRunDir}?after=${_livePanelOffset}`);
    const statusEl = document.getElementById('live-panel-status');
    const pulseEl = document.getElementById('live-pulse');

    const badgeEl = document.getElementById('live-panel-badge');
    if (data.is_live) {
      statusEl.textContent = 'streaming';
      statusEl.className = 'text-xs text-green-400 ml-auto';
      pulseEl.style.background = '#22c55e';
      badgeEl.textContent = 'Live';
      badgeEl.className = 'badge badge-open';
    } else {
      statusEl.textContent = 'completed';
      statusEl.className = 'text-xs text-gray-500 ml-auto';
      pulseEl.style.background = '#6b7280';
      pulseEl.style.animation = 'none';
      badgeEl.textContent = 'Complete';
      badgeEl.className = 'badge badge-closed';
      // Stop polling if not live and we've already loaded data
      if (_livePanelOffset > 0 && !data.entries.length) {
        clearInterval(_livePanelInterval);
        _livePanelInterval = null;
      }
    }

    if (data.offset !== undefined) {
      _livePanelOffset = data.offset;
    }

    if (data.entries && data.entries.length > 0) {
      _liveAppendEntries(data.entries);
    }
  } catch (err) {
    const statusEl = document.getElementById('live-panel-status');
    statusEl.textContent = 'error';
    statusEl.className = 'text-xs text-red-400 ml-auto';
  }
}

/**
 * Render parsed session entries into a container element.
 * Shared by the live panel and the trace view session log.
 */
// Map tool_id -> {tool_name, tool_headline} for linking results to calls
const _toolIdMap = {};

function _renderSessionEntries(entries, container) {
  for (const entry of entries) {
    const el = document.createElement('div');
    el.className = 'live-entry';

    const timeStr = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : '';

    if (entry.type === 'user') {
      el.className += ' live-entry-user';
      el.innerHTML = `
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs text-blue-400 font-semibold">USER</span>
          <span class="live-entry-time">${timeStr}</span>
        </div>`;
      const textEl = document.createElement('div');
      textEl.className = 'text-sm text-gray-300';
      textEl.textContent = entry.content;
      el.appendChild(textEl);

    } else if (entry.type === 'assistant_text') {
      el.className += ' live-entry-assistant';
      el.innerHTML = `
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs text-indigo-400 font-semibold">ASSISTANT</span>
          <span class="live-entry-time">${timeStr}</span>
        </div>`;
      const mdEl = renderMd(entry.content);
      mdEl.className += ' text-sm';
      el.appendChild(mdEl);

    } else if (entry.type === 'tool_use') {
      el.className += ' live-entry-tool';
      // Track tool_id for matching results
      if (entry.tool_id) {
        _toolIdMap[entry.tool_id] = {
          tool_name: entry.tool_name,
          tool_headline: entry.tool_headline || '',
        };
      }
      const details = document.createElement('details');
      details.className = 'text-sm';
      const headline = entry.tool_headline
        ? ' ' + entry.tool_headline.replace(/`([^`]+)`/g, '<code class="text-purple-300 bg-gray-800 px-1 rounded">$1</code>')
        : '';
      details.innerHTML = `
        <summary class="live-tool-toggle text-purple-400 text-xs font-mono cursor-pointer select-none">
          <strong>${escapeHtml(entry.tool_name)}</strong>${headline}
          <span class="live-entry-time ml-2">${timeStr}</span>
        </summary>
        <pre class="text-xs text-gray-400 mt-1 overflow-x-auto max-h-32 overflow-y-auto bg-gray-800 rounded p-2">${escapeHtml(entry.content)}</pre>`;
      el.appendChild(details);

    } else if (entry.type === 'tool_result') {
      el.className += ' live-entry-tool';
      const details = document.createElement('details');
      details.className = 'text-sm';
      const preview = (entry.content || '').slice(0, 80).replace(/\n/g, ' ');
      // Show which tool this result belongs to
      const caller = entry.tool_id ? _toolIdMap[entry.tool_id] : null;
      const resultLabel = caller
        ? `<span class="text-gray-400">${escapeHtml(caller.tool_name)}</span> result`
        : 'result';
      details.innerHTML = `
        <summary class="live-tool-toggle text-gray-500 text-xs font-mono cursor-pointer select-none">
          ${resultLabel} <span class="text-gray-600">${escapeHtml(preview)}${entry.content.length > 80 ? '...' : ''}</span>
          <span class="live-entry-time ml-2">${timeStr}</span>
        </summary>
        <pre class="text-xs text-gray-400 mt-1 overflow-x-auto max-h-48 overflow-y-auto bg-gray-800 rounded p-2">${escapeHtml(entry.content)}</pre>`;
      el.appendChild(details);

    } else if (entry.type === 'thinking') {
      el.className += ' live-entry-thinking';
      const details = document.createElement('details');
      details.className = 'text-sm';
      details.innerHTML = `
        <summary class="live-tool-toggle text-gray-600 text-xs">
          thinking...
          <span class="live-entry-time ml-2">${timeStr}</span>
        </summary>
        <div class="text-xs text-gray-500 mt-1 italic">${escapeHtml(entry.content)}</div>`;
      el.appendChild(details);

    } else {
      continue;
    }

    container.appendChild(el);
  }
}

function _liveAppendEntries(entries) {
  const container = document.getElementById('live-panel-entries');
  const body = document.getElementById('live-panel-body');

  _renderSessionEntries(entries, container);

  // Auto-scroll
  if (_livePanelAutoScroll) {
    body.scrollTop = body.scrollHeight;
  }
}

/**
 * Get a snippet (last assistant text, ~100 chars) for inline display.
 * Returns {text, timestamp, is_live} or null.
 */
async function getLiveSnippet(runDir) {
  try {
    const data = await api(`/api/dispatch/latest/${runDir}`);
    if (data.text || data.file_size_bytes) return data;
    return null;
  } catch {
    return null;
  }
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
  if (dispatchInterval) { clearInterval(dispatchInterval); dispatchInterval = null; }

  // Update active nav
  document.querySelectorAll('.nav-link').forEach(el => {
    el.classList.toggle('active', path.startsWith('/' + el.dataset.page));
  });

  if (path === '/' || path === '/beads') {
    renderBeads();
  } else if (path.startsWith('/dispatch/trace/')) {
    renderTrace(path.split('/dispatch/trace/')[1]);
  } else if (path === '/dispatch') {
    renderDispatch();
  } else if (path.startsWith('/bead/')) {
    renderBeadDetail(path.split('/bead/')[1]);
  } else if (path === '/sessions') {
    renderSessions();
  } else if (path === '/search') {
    const params = new URLSearchParams(window.location.search);
    renderSearch(params.get('q'), params.get('project'));
  } else if (path.startsWith('/source/')) {
    const params = new URLSearchParams(window.location.search);
    const turn = params.get('turn');
    if (turn) {
      renderContext(path.split('/source/')[1], parseInt(turn), parseInt(params.get('window') || '5'));
    } else {
      renderSource(path.split('/source/')[1]);
    }
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

// ── Nav Badges ───────────────────────────────────────────────

async function updateNavBadges() {
  try {
    const [ready, allBeads, sessions, terminals, dispatchStatus] = await Promise.all([
      api('/api/beads/ready'),
      api('/api/beads/list'),
      api('/api/active?threshold=600'),
      api('/api/terminals'),
      api('/api/dispatch/status'),
    ]);

    // Beads: ready count
    const readyCount = Array.isArray(ready) ? ready.length : 0;
    document.getElementById('badge-beads').textContent = readyCount || '';

    // Dispatch: running + queued counts from labels
    const beadList = Array.isArray(allBeads) ? allBeads : [];
    const runningStates = new Set(['running', 'launching', 'collecting', 'merging']);
    let runningCount = 0;
    let queuedCount = 0;
    for (const b of beadList) {
      const labels = b.labels || [];
      let dispatchLabel = null;
      for (const l of labels) {
        if (l.startsWith('dispatch:')) { dispatchLabel = l.split(':')[1]; break; }
      }
      if (dispatchLabel && runningStates.has(dispatchLabel)) {
        runningCount++;
      } else if (dispatchLabel === 'queued') {
        queuedCount++;
      } else if (!dispatchLabel && b.status === 'in_progress') {
        runningCount++;
      }
    }
    // Ground truth: count live agent containers (excludes persistent slack agents)
    const agentContainers = (dispatchStatus.containers || [])
      .filter(c => c.name.startsWith('agent-auto-'));
    runningCount = Math.max(runningCount, agentContainers.length);

    const dispatchEl = document.getElementById('badge-dispatch');
    let dispatchHtml = '';
    if (runningCount) dispatchHtml += `<span class="nav-badge nav-badge-green">▶${runningCount}</span>`;
    if (queuedCount) dispatchHtml += `<span class="nav-badge nav-badge-amber">◦${queuedCount}</span>`;
    dispatchEl.innerHTML = dispatchHtml;

    // Sessions: active count
    const sessionCount = Array.isArray(sessions) ? sessions.length : 0;
    document.getElementById('badge-sessions').textContent = sessionCount || '';

    // Terminal: open count
    const termCount = Array.isArray(terminals) ? terminals.length : 0;
    document.getElementById('badge-terminal').textContent = termCount || '';
  } catch (e) {
    // Silent fail — badges are non-critical
  }
}

// ── Init ─────────────────────────────────────────────────────

// Nav badges: poll every 5s
updateNavBadges();
setInterval(updateNavBadges, 5000);

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
