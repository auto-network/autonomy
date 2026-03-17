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

// ── Bead Search Helpers ──────────────────────────────────────

function highlightText(text, terms) {
  if (!terms.length) return text;
  // Escape regex special chars in search terms
  const escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const re = new RegExp(`(${escaped.join('|')})`, 'gi');
  return text.replace(re, '<mark class="bg-yellow-600 text-white rounded px-0.5">$1</mark>');
}

function filterBeads(issues, query) {
  if (!query) return issues;
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  return issues.filter(issue => {
    const haystack = `${issue.title || ''} ${issue.description || ''} ${issue.id || ''}`.toLowerCase();
    return terms.every(term => haystack.includes(term));
  });
}

let _beadsSearchTimer = null;
let _allBeads = [];

// ── View Switcher State ────────────────────────────────────

const _viewTabs = ['list', 'board', 'tree', 'deps'];
let _currentView = localStorage.getItem('beads-view') || 'board';
if (!_viewTabs.includes(_currentView)) _currentView = 'board';

// ── Table Sort & Bulk Selection State ──────────────────────

let _sortColumn = 'priority';
let _sortDirection = 'asc';
let _selectedBeadIds = new Set();

function sortBeads(beads, column, direction) {
  const mult = direction === 'asc' ? 1 : -1;
  return [...beads].sort((a, b) => {
    let va, vb;
    switch (column) {
      case 'title':
        va = (a.title || '').toLowerCase();
        vb = (b.title || '').toLowerCase();
        return mult * va.localeCompare(vb);
      case 'id':
        va = a.id || '';
        vb = b.id || '';
        return mult * va.localeCompare(vb);
      case 'priority':
        va = a.priority ?? 4;
        vb = b.priority ?? 4;
        return mult * (va - vb);
      case 'phase':
        const phaseOrder = { approved: 0, specified: 1, draft: 2, idea: 3 };
        va = phaseOrder[getPhase(a.labels)] ?? 4;
        vb = phaseOrder[getPhase(b.labels)] ?? 4;
        return mult * (va - vb);
      case 'type':
        va = (a.issue_type || '').toLowerCase();
        vb = (b.issue_type || '').toLowerCase();
        return mult * va.localeCompare(vb);
      case 'epic':
        va = (getEpicParent(a) || '').toLowerCase();
        vb = (getEpicParent(b) || '').toLowerCase();
        return mult * va.localeCompare(vb);
      case 'labels':
        va = (a.labels || []).filter(l => !l.startsWith('readiness:') && !l.startsWith('dispatch:')).join(',');
        vb = (b.labels || []).filter(l => !l.startsWith('readiness:') && !l.startsWith('dispatch:')).join(',');
        return mult * va.localeCompare(vb);
      case 'updated_at':
        va = a.updated_at || a.created_at || '';
        vb = b.updated_at || b.created_at || '';
        return mult * va.localeCompare(vb);
      case 'status':
        const statusOrder = { in_progress: 0, open: 1, blocked: 2, closed: 3 };
        va = statusOrder[a.status] ?? 4;
        vb = statusOrder[b.status] ?? 4;
        return mult * (va - vb);
      default:
        return 0;
    }
  });
}

window.sortByColumn = function(column) {
  if (_sortColumn === column) {
    _sortDirection = _sortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    _sortColumn = column;
    _sortDirection = 'asc';
  }
  renderBeadResults(globalSearch.value.trim());
};

window.toggleBeadSelect = function(id, event) {
  if (event) event.stopPropagation();
  if (_selectedBeadIds.has(id)) {
    _selectedBeadIds.delete(id);
  } else {
    _selectedBeadIds.add(id);
  }
  // Update checkbox UI without full re-render
  const cb = document.getElementById(`cb-${id}`);
  if (cb) cb.checked = _selectedBeadIds.has(id);
  // Update select-all checkbox
  updateSelectAllCheckbox();
  // Update bulk action bar visibility
  updateBulkActionBar();
};

window.toggleSelectAll = function(event) {
  const rows = document.querySelectorAll('.bead-table-row');
  if (_selectedBeadIds.size > 0) {
    // Deselect all
    _selectedBeadIds.clear();
  } else {
    // Select all visible
    rows.forEach(row => {
      const id = row.dataset.beadId;
      if (id) _selectedBeadIds.add(id);
    });
  }
  // Update all checkboxes
  rows.forEach(row => {
    const id = row.dataset.beadId;
    const cb = document.getElementById(`cb-${id}`);
    if (cb) cb.checked = _selectedBeadIds.has(id);
  });
  updateSelectAllCheckbox();
  updateBulkActionBar();
};

function updateSelectAllCheckbox() {
  const cb = document.getElementById('select-all-cb');
  if (!cb) return;
  const rows = document.querySelectorAll('.bead-table-row');
  const total = rows.length;
  const selected = _selectedBeadIds.size;
  cb.checked = total > 0 && selected === total;
  cb.indeterminate = selected > 0 && selected < total;
}

function updateBulkActionBar() {
  // Re-render filter bar container to swap between filter controls and bulk action toolbar
  const filterContainer = document.getElementById('filter-bar-container');
  if (filterContainer) filterContainer.innerHTML = renderFilterBar();
}

window.bulkApprove = async function() {
  const ids = [..._selectedBeadIds];
  for (const id of ids) {
    await approveBead(id);
  }
  _selectedBeadIds.clear();
  renderBeadResults(globalSearch.value.trim());
};

window.bulkSetPriority = function() {
  const picker = document.getElementById('bulk-priority-picker');
  if (picker) picker.classList.toggle('hidden');
  // Close label dropdown if open
  const labelDd = document.getElementById('bulk-label-dropdown');
  if (labelDd) labelDd.classList.add('hidden');
};

window.bulkApplyPriority = async function(priority) {
  const ids = [..._selectedBeadIds];
  alert(`Set priority P${priority} for ${ids.length} beads: ${ids.join(', ')}\n(Requires bd update - not available in read-only mode)`);
  const picker = document.getElementById('bulk-priority-picker');
  if (picker) picker.classList.add('hidden');
};

window.toggleBulkLabelDropdown = function() {
  const dd = document.getElementById('bulk-label-dropdown');
  if (dd) dd.classList.toggle('hidden');
  // Close priority picker if open
  const picker = document.getElementById('bulk-priority-picker');
  if (picker) picker.classList.add('hidden');
};

window.bulkApplyExistingLabel = function(label) {
  const ids = [..._selectedBeadIds];
  alert(`Add label "${label}" to ${ids.length} beads: ${ids.join(', ')}\n(Requires bd update - not available in read-only mode)`);
  const dd = document.getElementById('bulk-label-dropdown');
  if (dd) dd.classList.add('hidden');
};

window.bulkApplyNewLabel = function(label) {
  if (!label || !label.trim()) return;
  const ids = [..._selectedBeadIds];
  alert(`Add label "${label.trim()}" to ${ids.length} beads: ${ids.join(', ')}\n(Requires bd update - not available in read-only mode)`);
  const dd = document.getElementById('bulk-label-dropdown');
  if (dd) dd.classList.add('hidden');
};

window.bulkClearSelection = function() {
  _selectedBeadIds.clear();
  // Update all checkboxes
  document.querySelectorAll('.bead-table-row').forEach(row => {
    const id = row.dataset.beadId;
    const cb = document.getElementById(`cb-${id}`);
    if (cb) cb.checked = false;
  });
  updateSelectAllCheckbox();
  updateBulkActionBar();
};

// ── Click-outside-to-close for toolbar dropdowns ────────────
document.addEventListener('click', function(e) {
  // Close priority picker
  const picker = document.getElementById('bulk-priority-picker');
  if (picker && !picker.classList.contains('hidden')) {
    if (!e.target.closest('#bulk-priority-wrap')) {
      picker.classList.add('hidden');
    }
  }
  // Close bulk label dropdown
  const labelDd = document.getElementById('bulk-label-dropdown');
  const labelWrap = document.getElementById('bulk-label-dropdown-wrap');
  if (labelDd && labelWrap && !labelDd.classList.contains('hidden')) {
    if (!labelWrap.contains(e.target)) {
      labelDd.classList.add('hidden');
    }
  }
});

// ── Filter State & URL Persistence ─────────────────────────

const _defaultFilters = {
  priority: [],    // e.g. [0, 1]
  phase: [],       // e.g. ['idea', 'draft', 'specified', 'approved']
  type: [],        // e.g. ['epic', 'task', 'bug', 'feature']
  labels: [],      // arbitrary label strings
  labelMode: 'or', // 'and' | 'or'
  epic: '',        // parent epic id
  blocked: '',     // 'yes' | 'no' | ''
};

let _filters = { ..._defaultFilters, priority: [], phase: [], type: [], labels: [] };

function filtersFromURL() {
  const p = new URLSearchParams(window.location.search);
  // Restore view from URL param, fallback to localStorage, then 'list'
  const urlView = p.get('view');
  if (urlView && _viewTabs.includes(urlView)) {
    _currentView = urlView;
    localStorage.setItem('beads-view', _currentView);
  }
  return {
    priority: p.get('priority') ? p.get('priority').split(',').map(Number) : [],
    phase: p.get('phase') ? p.get('phase').split(',') : [],
    type: p.get('type') ? p.get('type').split(',') : [],
    labels: p.get('labels') ? p.get('labels').split(',') : [],
    labelMode: p.get('labelMode') || 'or',
    epic: p.get('epic') || '',
    blocked: p.get('blocked') || '',
  };
}

function filtersToURL(f) {
  const p = new URLSearchParams();
  if (_currentView !== 'board') p.set('view', _currentView);
  if (f.priority.length) p.set('priority', f.priority.join(','));
  if (f.phase.length) p.set('phase', f.phase.join(','));
  if (f.type.length) p.set('type', f.type.join(','));
  if (f.labels.length) p.set('labels', f.labels.join(','));
  if (f.labelMode !== 'or') p.set('labelMode', f.labelMode);
  if (f.epic) p.set('epic', f.epic);
  if (f.blocked) p.set('blocked', f.blocked);
  const qs = p.toString();
  const newUrl = window.location.pathname + (qs ? '?' + qs : '');
  history.replaceState({}, '', newUrl);
}

function hasActiveFilters(f) {
  return f.priority.length || f.phase.length || f.type.length || f.labels.length || f.epic || f.blocked;
}

function getPhase(labels) {
  for (const l of labels || []) {
    if (l.startsWith('readiness:')) return l.split(':')[1];
  }
  return null;
}

function isBlocked(issue) {
  return issue.status === 'open' && issue.dependencies?.some(d => d.status !== 'closed');
}

function getEpicParent(issue) {
  // Check if issue has a parent-child dependency where parent is an epic
  for (const d of issue.dependencies || []) {
    if (d.type === 'parent-child') return d.depends_on_id;
  }
  return null;
}

function applyFilters(issues, filters) {
  return issues.filter(issue => {
    // Priority filter
    if (filters.priority.length && !filters.priority.includes(issue.priority)) return false;
    // Phase filter
    if (filters.phase.length) {
      const phase = getPhase(issue.labels);
      if (!phase || !filters.phase.includes(phase)) return false;
    }
    // Type filter
    if (filters.type.length && !filters.type.includes(issue.issue_type)) return false;
    // Label filter
    if (filters.labels.length) {
      const issueLabels = issue.labels || [];
      if (filters.labelMode === 'and') {
        if (!filters.labels.every(l => issueLabels.includes(l))) return false;
      } else {
        if (!filters.labels.some(l => issueLabels.includes(l))) return false;
      }
    }
    // Epic filter
    if (filters.epic) {
      const parent = getEpicParent(issue);
      if (parent !== filters.epic && issue.id !== filters.epic) return false;
    }
    // Blocked filter
    if (filters.blocked === 'yes' && !isBlocked(issue)) return false;
    if (filters.blocked === 'no' && isBlocked(issue)) return false;
    return true;
  });
}

function collectAllLabels(issues) {
  const set = new Set();
  for (const i of issues) {
    for (const l of i.labels || []) {
      if (!l.startsWith('readiness:') && !l.startsWith('dispatch:')) set.add(l);
    }
  }
  return [...set].sort();
}

function collectEpics(issues) {
  return issues.filter(i => i.issue_type === 'epic' && i.status !== 'closed');
}

function toggleInArray(arr, val) {
  const idx = arr.indexOf(val);
  if (idx >= 0) arr.splice(idx, 1);
  else arr.push(val);
  return arr;
}

function renderLabelDropdown(mode) {
  // mode: 'filter' (in filter bar with AND/OR toggle) or 'action' (bulk add with text input)
  const allLabels = collectAllLabels(_allBeads);
  if (!allLabels.length && mode === 'filter') return '';

  const f = _filters;
  const isAction = mode === 'action';
  const wrapperId = isAction ? 'bulk-label-dropdown-wrap' : 'label-dropdown-wrap';
  const dropdownId = isAction ? 'bulk-label-dropdown' : 'label-dropdown';

  // Header: AND/OR toggle for filter mode, text input for action mode
  const headerHtml = isAction
    ? `<div class="p-1.5 border-b border-gray-700">
        <input type="text" id="bulk-label-input" placeholder="Type new label..."
               class="w-full px-2 py-1 bg-gray-700 text-gray-100 text-xs rounded border border-gray-600 focus:outline-none focus:border-indigo-500"
               onclick="event.stopPropagation()"
               onkeydown="if(event.key==='Enter'){event.preventDefault();bulkApplyNewLabel(this.value)}">
      </div>`
    : `<div class="p-1 border-b border-gray-700 flex items-center gap-1">
        <span class="text-xs text-gray-400">Mode:</span>
        <button class="px-1.5 py-0.5 rounded text-xs ${f.labelMode === 'or' ? 'bg-indigo-600 text-white' : 'bg-gray-700 text-gray-300'}"
                onclick="toggleFilter('labelMode','or')">OR</button>
        <button class="px-1.5 py-0.5 rounded text-xs ${f.labelMode === 'and' ? 'bg-indigo-600 text-white' : 'bg-gray-700 text-gray-300'}"
                onclick="toggleFilter('labelMode','and')">AND</button>
      </div>`;

  // Label list: checkboxes for filter mode, clickable items for action mode
  const labelsHtml = allLabels.map(l => {
    if (isAction) {
      return `<div class="flex items-center gap-2 px-2 py-1 hover:bg-gray-700 cursor-pointer text-xs"
                   onclick="bulkApplyExistingLabel('${l.replace(/'/g, "\\'")}')">
        <span class="truncate">${l}</span>
      </div>`;
    }
    return `<label class="flex items-center gap-2 px-2 py-1 hover:bg-gray-700 cursor-pointer text-xs">
      <input type="checkbox" ${f.labels.includes(l) ? 'checked' : ''}
             onchange="toggleFilter('label','${l.replace(/'/g, "\\'")}')" class="rounded">
      <span class="truncate">${l}</span>
    </label>`;
  }).join('');

  const toggleFn = isAction ? `toggleBulkLabelDropdown()` : `document.getElementById('${dropdownId}').classList.toggle('hidden')`;
  const btnLabel = isAction
    ? 'Add Label ▾'
    : `Labels${f.labels.length ? ` (${f.labels.length})` : ''} ▾`;
  const btnClass = isAction
    ? 'px-3 py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs rounded font-semibold transition-colors'
    : 'px-2 py-0.5 rounded text-xs font-medium bg-gray-700 text-gray-300 hover:bg-gray-600';

  return `
    <div class="relative inline-block" id="${wrapperId}">
      <button class="${btnClass}" onclick="${toggleFn}">${btnLabel}</button>
      <div id="${dropdownId}" class="hidden absolute z-20 mt-1 bg-gray-800 border border-gray-600 rounded shadow-lg max-h-48 overflow-y-auto w-48">
        ${headerHtml}
        ${labelsHtml}
      </div>
    </div>`;
}

function renderFilterBar() {
  // When items are selected in list view, show bulk action toolbar instead
  if (_selectedBeadIds.size > 0 && _currentView === 'list') {
    return renderBulkActionToolbar();
  }

  const allLabels = collectAllLabels(_allBeads);
  const epics = collectEpics(_allBeads);
  const f = _filters;
  const active = hasActiveFilters(f);

  function chip(label, isActive, onclick) {
    const cls = isActive
      ? 'bg-indigo-600 text-white'
      : 'bg-gray-700 text-gray-300 hover:bg-gray-600';
    return `<button class="px-2 py-0.5 rounded-full text-xs font-medium ${cls} transition-colors" onclick="${onclick}">${label}</button>`;
  }

  // Priority chips
  const priorityChips = [0,1,2,3,4].map(p =>
    chip(`P${p}`, f.priority.includes(p), `toggleFilter('priority',${p})`)
  ).join('');

  // Phase chips
  const phases = ['idea','draft','specified','approved'];
  const phaseChips = phases.map(p =>
    chip(p, f.phase.includes(p), `toggleFilter('phase','${p}')`)
  ).join('');

  // Type chips
  const types = ['epic','task','bug','feature'];
  const typeChips = types.map(t =>
    chip(t, f.type.includes(t), `toggleFilter('type','${t}')`)
  ).join('');

  // Blocked toggle
  const blockedChips =
    chip('Blocked', f.blocked === 'yes', `toggleFilter('blocked','yes')`) +
    chip('Not blocked', f.blocked === 'no', `toggleFilter('blocked','no')`);

  // Labels dropdown (filter mode)
  const labelDropdown = renderLabelDropdown('filter');

  // Epic dropdown
  const epicDropdown = epics.length ? `
    <select class="px-2 py-0.5 rounded text-xs bg-gray-700 text-gray-300 border-none focus:ring-1 focus:ring-indigo-500"
            onchange="toggleFilter('epic', this.value)">
      <option value="">All epics</option>
      ${epics.map(e => `<option value="${e.id}" ${f.epic === e.id ? 'selected' : ''}>${e.title}</option>`).join('')}
    </select>` : '';

  // Active filter chips (removable)
  let activeChips = '';
  if (active) {
    const chips = [];
    for (const p of f.priority) chips.push(`<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-900 text-indigo-200 text-xs rounded-full">P${p}<button class="ml-0.5 hover:text-white" onclick="toggleFilter('priority',${p})">&times;</button></span>`);
    for (const p of f.phase) chips.push(`<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-900 text-indigo-200 text-xs rounded-full">${p}<button class="ml-0.5 hover:text-white" onclick="toggleFilter('phase','${p}')">&times;</button></span>`);
    for (const t of f.type) chips.push(`<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-900 text-indigo-200 text-xs rounded-full">${t}<button class="ml-0.5 hover:text-white" onclick="toggleFilter('type','${t}')">&times;</button></span>`);
    for (const l of f.labels) chips.push(`<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-900 text-indigo-200 text-xs rounded-full">${l}<button class="ml-0.5 hover:text-white" onclick="toggleFilter('label','${l.replace(/'/g, "\\'")}')">&times;</button></span>`);
    if (f.blocked) chips.push(`<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-900 text-indigo-200 text-xs rounded-full">${f.blocked === 'yes' ? 'Blocked' : 'Not blocked'}<button class="ml-0.5 hover:text-white" onclick="toggleFilter('blocked','')">&times;</button></span>`);
    if (f.epic) {
      const epicName = _allBeads.find(b => b.id === f.epic)?.title || f.epic;
      chips.push(`<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-900 text-indigo-200 text-xs rounded-full">Epic: ${epicName}<button class="ml-0.5 hover:text-white" onclick="toggleFilter('epic','')">&times;</button></span>`);
    }
    activeChips = `
      <div class="flex items-center gap-1 flex-wrap mt-2">
        <span class="text-xs text-gray-500">Active:</span>
        ${chips.join('')}
        <button class="text-xs text-gray-400 hover:text-white ml-1 underline" onclick="clearAllFilters()">Clear all</button>
      </div>`;
  }

  return `
    <div class="mb-4 space-y-2" id="filter-bar">
      <div class="flex items-center gap-3 flex-wrap text-xs">
        <span class="text-gray-500 font-medium">Priority</span>
        <div class="flex gap-1">${priorityChips}</div>
        <span class="text-gray-600">|</span>
        <span class="text-gray-500 font-medium">Phase</span>
        <div class="flex gap-1">${phaseChips}</div>
        <span class="text-gray-600">|</span>
        <span class="text-gray-500 font-medium">Type</span>
        <div class="flex gap-1">${typeChips}</div>
        <span class="text-gray-600">|</span>
        <div class="flex gap-1">${blockedChips}</div>
        <span class="text-gray-600">|</span>
        ${labelDropdown}
        ${epicDropdown}
      </div>
      ${activeChips}
    </div>`;
}

function renderBulkActionToolbar() {
  const labelDropdown = renderLabelDropdown('action');

  return `
    <div class="mb-4" id="filter-bar">
      <div class="bulk-action-bar">
        <span class="text-sm font-medium">${_selectedBeadIds.size} selected</span>
        <button onclick="bulkApprove()" class="px-3 py-1 bg-green-700 hover:bg-green-600 text-white text-xs rounded font-semibold transition-colors">Approve</button>
        <div class="relative" id="bulk-priority-wrap">
          <button onclick="bulkSetPriority()" class="px-3 py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs rounded font-semibold transition-colors">Set Priority ▾</button>
          <div id="bulk-priority-picker" class="hidden absolute z-20 mt-1 left-0 bg-gray-800 border border-gray-600 rounded shadow-lg p-1 flex gap-1">
            ${[0,1,2,3,4].map(p => `<button onclick="bulkApplyPriority(${p})" class="px-2 py-0.5 rounded text-xs font-semibold hover:bg-gray-600 transition-colors">P${p}</button>`).join('')}
          </div>
        </div>
        ${labelDropdown}
        <button onclick="bulkClearSelection()" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs rounded font-semibold transition-colors ml-auto">Clear Selection</button>
      </div>
    </div>`;
}

// Global filter toggle handler
window.toggleFilter = function(dimension, value) {
  if (dimension === 'priority') {
    toggleInArray(_filters.priority, value);
  } else if (dimension === 'phase') {
    toggleInArray(_filters.phase, value);
  } else if (dimension === 'type') {
    toggleInArray(_filters.type, value);
  } else if (dimension === 'label') {
    toggleInArray(_filters.labels, value);
  } else if (dimension === 'labelMode') {
    _filters.labelMode = value;
  } else if (dimension === 'epic') {
    _filters.epic = value;
  } else if (dimension === 'blocked') {
    _filters.blocked = _filters.blocked === value ? '' : value;
  }
  filtersToURL(_filters);
  renderBeadResults(globalSearch.value.trim());
};

window.clearAllFilters = function() {
  _filters = { ..._defaultFilters, priority: [], phase: [], type: [], labels: [] };
  filtersToURL(_filters);
  renderBeadResults(globalSearch.value.trim());
};

// Close label dropdown when clicking outside
document.addEventListener('click', (e) => {
  const wrap = document.getElementById('label-dropdown-wrap');
  const dd = document.getElementById('label-dropdown');
  if (dd && wrap && !wrap.contains(e.target)) {
    dd.classList.add('hidden');
  }
});

// ── View Switcher ──────────────────────────────────────────

function renderViewSwitcher() {
  const icons = { list: '▤', board: '▦', tree: '🌳', deps: '🔗' };
  const labels = { list: 'List', board: 'Board', tree: 'Tree', deps: 'Deps' };
  const tabs = _viewTabs.map(v => {
    const active = v === _currentView;
    const cls = active
      ? 'border-indigo-500 text-indigo-400'
      : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-500';
    return `<button class="px-3 py-2 text-sm font-medium border-b-2 ${cls} transition-colors"
                    onclick="switchView('${v}')">${icons[v]} ${labels[v]}</button>`;
  }).join('');
  return `<div class="flex gap-1 border-b border-gray-700 mb-4" id="view-switcher">${tabs}</div>`;
}

window.switchView = function(view) {
  if (!_viewTabs.includes(view) || view === _currentView) return;
  _currentView = view;
  _selectedBeadIds.clear();
  localStorage.setItem('beads-view', view);
  filtersToURL(_filters);
  // Re-render switcher + results without re-fetching data
  const switcherContainer = document.getElementById('view-switcher-container');
  if (switcherContainer) switcherContainer.innerHTML = renderViewSwitcher();
  renderBeadResults(globalSearch.value.trim());
};

async function renderBeads() {
  pageTitle.textContent = 'Beads';
  const data = await api('/api/beads/list');
  if (data.error) {
    content.innerHTML = `<div class="text-red-400">${data.error}</div>`;
    return;
  }
  _allBeads = Array.isArray(data) ? data : [];

  // Restore filters from URL
  _filters = filtersFromURL();

  // Build view switcher + filter bar + results container
  content.innerHTML = `
    <div id="view-switcher-container">${renderViewSwitcher()}</div>
    <div id="filter-bar-container"></div>
    <div id="bead-results"></div>`;

  // Render with current global search query
  renderBeadResults(globalSearch.value.trim());

  // Debounced search via global input (300ms)
  globalSearch.addEventListener('input', _beadsSearchHandler);
}

function _beadsSearchHandler() {
  if (_beadsSearchTimer) clearTimeout(_beadsSearchTimer);
  _beadsSearchTimer = setTimeout(() => {
    renderBeadResults(globalSearch.value.trim());
  }, 300);
}

function renderBeadResults(query) {
  const container = document.getElementById('bead-results');
  if (!container) return;

  // Render filter bar
  const filterContainer = document.getElementById('filter-bar-container');
  if (filterContainer) filterContainer.innerHTML = renderFilterBar();

  // Apply text search then structured filters
  const textFiltered = filterBeads(_allBeads, query);
  const filtered = applyFilters(textFiltered, _filters);
  const terms = query ? query.toLowerCase().split(/\s+/).filter(Boolean) : [];

  // Empty state (shared across all views)
  const hasFilters = hasActiveFilters(_filters);
  if ((query || hasFilters) && !filtered.length) {
    container.innerHTML = `
      <div class="text-center py-12 text-gray-400">
        <div class="text-4xl mb-3">🔍</div>
        <div class="text-lg mb-1">No beads match ${query ? `"${query.replace(/</g, '&lt;')}"` : 'the active filters'}</div>
        <div class="text-sm">${hasFilters ? 'Try removing some filters or ' : ''}Try different keywords or check spelling</div>
      </div>`;
    return;
  }

  // Search result count when filtering (shared across all views)
  const countHtml = (query || hasFilters)
    ? `<div class="text-sm text-gray-400 mb-3">${filtered.length} bead${filtered.length !== 1 ? 's' : ''}${query ? ` matching "${query.replace(/</g, '&lt;')}"` : ''}${hasFilters ? ' (filtered)' : ''}</div>`
    : '';

  // Dispatch to view-specific renderer
  if (_currentView === 'board') {
    container.innerHTML = countHtml + renderBoardView(filtered, terms, query);
  } else if (_currentView === 'tree') {
    container.innerHTML = countHtml + renderTreeView(filtered, terms, query);
  } else if (_currentView === 'deps') {
    container.innerHTML = countHtml + renderDepsView(filtered, terms, query);
  } else {
    container.innerHTML = countHtml + renderListView(filtered, terms, query);
  }
}

// ── Shared Bead Rendering Helpers ──────────────────────────

function renderIssueRow(issue, terms, query) {
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
  const titleHtml = highlightText(issue.title || '', terms);
  let descHtml = '';
  if (query && issue.description) {
    const desc = issue.description.length > 120 ? issue.description.slice(0, 120) + '...' : issue.description;
    descHtml = `<div class="text-xs text-gray-400 mt-1 truncate">${highlightText(desc, terms)}</div>`;
  }
  return `
    <div class="p-4 sm:p-3 bg-gray-800 rounded-lg hover:bg-gray-750 cursor-pointer border border-gray-700"
         onclick="navigateTo('/bead/${issue.id}')">
      <div class="flex items-center gap-2 mb-1 sm:mb-0">
        <span>${type}</span>
        <span class="truncate text-sm sm:text-base">${titleHtml}</span>
      </div>
      ${descHtml}
      <div class="flex items-center gap-2 flex-wrap mt-1 sm:mt-0">
        <span class="font-mono text-xs text-gray-500">${issue.id}</span>
        ${priorityBadge(issue.priority)}
        ${approveHtml}
      </div>
    </div>`;
}

function renderSection(title, items, terms, query, defaultOpen = true) {
  if (!items.length) return '';
  return `
    <details ${defaultOpen ? 'open' : ''} class="mb-6">
      <summary class="text-lg font-semibold mb-3 cursor-pointer">${title} <span class="text-gray-500">(${items.length})</span></summary>
      <div class="space-y-2">${items.map(i => renderIssueRow(i, terms, query)).join('')}</div>
    </details>`;
}

// ── List View (dense sortable table) ───────────────────────

function renderListView(filtered, terms, query) {
  // Build epic title lookup
  const epicTitleMap = {};
  for (const b of _allBeads) {
    if (b.issue_type === 'epic') epicTitleMap[b.id] = b.title;
  }

  // Sort
  const sorted = sortBeads(filtered, _sortColumn, _sortDirection);

  // Column definitions
  const columns = [
    { key: 'title',      label: 'Title' },
    { key: 'id',         label: 'ID' },
    { key: 'priority',   label: 'Pri' },
    { key: 'phase',      label: 'Phase' },
    { key: 'type',       label: 'Type' },
    { key: 'epic',       label: 'Epic' },
    { key: 'labels',     label: 'Labels' },
    { key: 'updated_at', label: 'Updated' },
  ];

  function sortArrow(key) {
    if (_sortColumn !== key) return '<span class="text-gray-600 ml-0.5">&#x2195;</span>';
    return _sortDirection === 'asc'
      ? '<span class="text-indigo-400 ml-0.5">&#x25B2;</span>'
      : '<span class="text-indigo-400 ml-0.5">&#x25BC;</span>';
  }

  // Header row
  const headerCells = `
    <th class="bead-th bead-th-cb p-0"><label class="flex items-center justify-center w-10 h-10 cursor-pointer"><input type="checkbox" id="select-all-cb" onclick="toggleSelectAll(event)" class="rounded cursor-pointer size-4"></label></th>
    ${columns.map(c => `
      <th class="bead-th bead-th-${c.key}" onclick="sortByColumn('${c.key}')" title="Sort by ${c.label}">
        ${c.label}${sortArrow(c.key)}
      </th>
    `).join('')}`;

  // Body rows
  const rows = sorted.map((issue, idx) => {
    const titleHtml = highlightText(issue.title || '', terms);
    const phase = getPhase(issue.labels) || '';
    const epicId = getEpicParent(issue);
    const epicTitle = epicId ? (epicTitleMap[epicId] || epicId) : '';
    const visibleLabels = (issue.labels || []).filter(l =>
      !l.startsWith('readiness:') && !l.startsWith('dispatch:')
    );
    const labelChips = visibleLabels.map(l =>
      `<span class="px-1 py-0 bg-gray-700 text-gray-300 text-xs rounded whitespace-nowrap">${l}</span>`
    ).join(' ');
    const updatedRaw = issue.updated_at || issue.created_at || '';
    const updatedDate = updatedRaw ? new Date(updatedRaw) : null;
    const updatedDisplay = updatedDate
      ? updatedDate.getFullYear() + '-' + String(updatedDate.getMonth()+1).padStart(2,'0') + '-' + String(updatedDate.getDate()).padStart(2,'0') + ' ' + String(updatedDate.getHours()).padStart(2,'0') + ':' + String(updatedDate.getMinutes()).padStart(2,'0')
      : '';
    const checked = _selectedBeadIds.has(issue.id) ? 'checked' : '';
    const rowBg = idx % 2 === 0 ? 'bead-tr-even' : 'bead-tr-odd';

    return `
      <tr class="bead-table-row ${rowBg} hover:bg-gray-700 cursor-pointer transition-colors"
          data-bead-id="${issue.id}"
          onclick="navigateTo('/bead/${issue.id}')">
        <td class="bead-td bead-td-cb p-0" onclick="event.stopPropagation()">
          <label class="flex items-center justify-center w-10 h-10 cursor-pointer">
            <input type="checkbox" id="cb-${issue.id}" ${checked}
                   onclick="toggleBeadSelect('${issue.id}', event)" class="rounded cursor-pointer size-4">
          </label>
        </td>
        <td class="bead-td bead-td-title">${titleHtml}</td>
        <td class="bead-td bead-td-id font-mono text-xs text-gray-400">${issue.id}</td>
        <td class="bead-td bead-td-priority">${priorityBadge(issue.priority)}</td>
        <td class="bead-td bead-td-phase text-xs">${phase}</td>
        <td class="bead-td bead-td-type text-xs">${issue.issue_type || ''}</td>
        <td class="bead-td bead-td-epic text-xs text-gray-400 truncate max-w-[120px]" title="${epicTitle}">${epicTitle}</td>
        <td class="bead-td bead-td-labels">${labelChips}</td>
        <td class="bead-td bead-td-updated text-xs text-gray-500 whitespace-nowrap">${updatedDisplay}</td>
      </tr>`;
  }).join('');

  return `
    <div class="overflow-x-auto">
      <table class="bead-table w-full">
        <thead><tr>${headerCells}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ── Board View (Kanban columns by readiness phase) ─────────

function renderBoardView(filtered, terms, query) {
  // Only show open/in_progress beads — closed beads don't belong on the board
  const active = filtered.filter(i => i.status !== 'closed');

  // Bucket by readiness phase
  const buckets = { idea: [], draft: [], specified: [], approved: [] };
  for (const issue of active) {
    const phase = getPhase(issue.labels) || 'idea';
    if (buckets[phase]) buckets[phase].push(issue);
    else buckets.idea.push(issue); // unknown phase → idea
  }

  const columns = [
    { key: 'idea',      title: 'Ideas',     color: 'border-yellow-500', items: buckets.idea },
    { key: 'draft',     title: 'Drafts',    color: 'border-blue-500',   items: buckets.draft },
    { key: 'specified', title: 'Specified',  color: 'border-purple-500', items: buckets.specified },
    { key: 'approved',  title: 'Approved',   color: 'border-green-500',  items: buckets.approved },
  ];

  // Build a lookup for epic titles
  const epicTitleMap = {};
  for (const b of _allBeads) {
    if (b.issue_type === 'epic') epicTitleMap[b.id] = b.title;
  }

  function renderBoardCard(issue, colKey) {
    const typeIcon = issue.issue_type === 'epic' ? '📦'
      : issue.issue_type === 'bug' ? '🐛' : '📋';
    const titleHtml = highlightText(issue.title || '', terms);

    // Epic parent name
    const epicParentId = getEpicParent(issue);
    const epicHtml = epicParentId && epicTitleMap[epicParentId]
      ? `<div class="text-xs text-gray-500 truncate mb-1">${epicTitleMap[epicParentId]}</div>`
      : '';

    // Label chips (exclude readiness:* since column implies it)
    const visibleLabels = (issue.labels || []).filter(l =>
      !l.startsWith('readiness:') && !l.startsWith('dispatch:')
    );
    const labelChips = visibleLabels.map(l =>
      `<span class="px-1.5 py-0.5 bg-gray-700 text-gray-300 text-xs rounded">${l}</span>`
    ).join('');

    // Approve button — only on specified column cards
    let approveHtml = '';
    if (colKey === 'specified') {
      approveHtml = `<button id="approve-btn-${issue.id}"
        onclick="event.stopPropagation(); approveBead('${issue.id}', event)"
        class="mt-2 w-full px-2 py-1 bg-green-700 hover:bg-green-600 text-white text-xs rounded font-semibold transition-colors">Approve</button>`;
    }

    return `
      <div class="p-3 bg-gray-800 rounded-lg cursor-pointer border border-gray-700 hover:border-gray-500 transition-colors"
           onclick="navigateTo('/bead/${issue.id}')">
        ${epicHtml}
        <div class="flex items-start gap-2 mb-2">
          <span class="flex-shrink-0">${typeIcon}</span>
          <span class="text-sm leading-snug">${titleHtml}</span>
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="font-mono text-xs text-gray-500">${issue.id}</span>
          ${priorityBadge(issue.priority)}
          ${labelChips}
        </div>
        ${approveHtml}
      </div>`;
  }

  const cols = columns.map(col => `
    <div class="board-column flex-1 min-w-[260px] flex flex-col">
      <div class="border-t-2 ${col.color} pt-2 mb-3 flex-shrink-0">
        <h3 class="text-sm font-semibold text-gray-300">${col.title} <span class="text-gray-500">(${col.items.length})</span></h3>
      </div>
      <div class="space-y-2 overflow-y-auto flex-1 pr-1 board-column-scroll">
        ${col.items.map(i => renderBoardCard(i, col.key)).join('') || '<div class="text-xs text-gray-600 italic py-2">No beads</div>'}
      </div>
    </div>
  `).join('');

  return `<div class="board-container flex gap-4 overflow-x-auto pb-4">${cols}</div>`;
}

// ── Tree View (epics with children) ────────────────────────

let _treeExpanded = true; // expand-all state

function treeTypeIcon(type) {
  switch (type) {
    case 'epic': return '📦';
    case 'bug': return '🐛';
    case 'feature': return '✨';
    case 'task': return '📋';
    default: return '📋';
  }
}

function treePhaseBadge(labels) {
  const phase = getPhase(labels);
  if (!phase) return '';
  const colors = {
    idea: 'bg-gray-600 text-gray-200',
    draft: 'bg-blue-900 text-blue-300',
    specified: 'bg-indigo-900 text-indigo-300',
    approved: 'bg-green-900 text-green-300',
  };
  const cls = colors[phase] || 'bg-gray-700 text-gray-300';
  return `<span class="inline-block px-1.5 py-0.5 rounded text-xs font-medium ${cls}">${phase}</span>`;
}

function treeProgressBar(children, allBeads) {
  // Count closed children from full dataset (not just filtered)
  if (!children.length) return '';
  const total = children.length;
  const closed = children.filter(c => c.status === 'closed').length;
  const pct = Math.round((closed / total) * 100);
  const barColor = pct === 100 ? 'bg-green-500' : pct > 50 ? 'bg-indigo-500' : 'bg-amber-500';
  return `
    <div class="inline-flex items-center gap-1.5 ml-2">
      <div class="w-20 h-1.5 bg-gray-700 rounded-full overflow-hidden">
        <div class="${barColor} h-full rounded-full transition-all" style="width:${pct}%"></div>
      </div>
      <span class="text-xs text-gray-400">${closed}/${total}</span>
    </div>`;
}

function renderTreeNode(issue, terms, query) {
  const icon = treeTypeIcon(issue.issue_type);
  const titleHtml = highlightText(issue.title || '', terms);
  const phase = treePhaseBadge(issue.labels);
  let descHtml = '';
  if (query && issue.description) {
    const desc = issue.description.length > 120 ? issue.description.slice(0, 120) + '...' : issue.description;
    descHtml = `<div class="text-xs text-gray-500 mt-0.5 truncate">${highlightText(desc, terms)}</div>`;
  }
  return `
    <div class="tree-node flex items-start gap-2 py-1.5 px-2 rounded hover:bg-gray-800 cursor-pointer group"
         onclick="navigateTo('/bead/${issue.id}')">
      <span class="text-sm flex-shrink-0 mt-0.5">${icon}</span>
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="text-sm truncate">${titleHtml}</span>
          <span class="font-mono text-xs text-gray-600">${issue.id}</span>
          ${priorityBadge(issue.priority)}
          ${phase}
        </div>
        ${descHtml}
      </div>
    </div>`;
}

function renderTreeView(filtered, terms, query) {
  // Group beads by epic parent
  const epicMap = new Map();   // epicId -> { epic, children }
  const orphans = [];          // beads with no epic parent

  const filteredIds = new Set(filtered.map(i => i.id));

  for (const issue of filtered) {
    const parent = getEpicParent(issue);
    if (parent && issue.issue_type !== 'epic') {
      if (!epicMap.has(parent)) epicMap.set(parent, { epic: null, children: [] });
      epicMap.get(parent).children.push(issue);
    } else if (issue.issue_type === 'epic') {
      if (!epicMap.has(issue.id)) epicMap.set(issue.id, { epic: issue, children: [] });
      else epicMap.get(issue.id).epic = issue;
    } else {
      orphans.push(issue);
    }
  }

  // Collapse empty branches when filtering
  const hasFilters = !!(query || hasActiveFilters(_filters));
  for (const [epicId, group] of epicMap) {
    if (hasFilters && !group.children.length && (!group.epic || !filteredIds.has(epicId))) {
      epicMap.delete(epicId);
    }
  }

  // Also gather ALL children per epic from full dataset for progress calculation
  const allChildrenByEpic = new Map();
  for (const b of _allBeads) {
    const parent = getEpicParent(b);
    if (parent && b.issue_type !== 'epic') {
      if (!allChildrenByEpic.has(parent)) allChildrenByEpic.set(parent, []);
      allChildrenByEpic.get(parent).push(b);
    }
  }

  const openAttr = _treeExpanded ? 'open' : '';

  // Expand/collapse toggle
  let html = `
    <div class="flex items-center gap-2 mb-3">
      <button onclick="treeToggleAll()" class="text-xs px-2 py-1 bg-gray-800 border border-gray-700 rounded hover:bg-gray-700 text-gray-300"
              id="tree-toggle-btn">${_treeExpanded ? 'Collapse all' : 'Expand all'}</button>
      <span class="text-xs text-gray-500">${epicMap.size} epic${epicMap.size !== 1 ? 's' : ''}${orphans.length ? `, ${orphans.length} ungrouped` : ''}</span>
    </div>`;

  // Render each epic group
  for (const [epicId, group] of epicMap) {
    const epic = group.epic || _allBeads.find(b => b.id === epicId);
    const epicTitle = epic ? highlightText(epic.title, terms) : epicId;
    const epicPhase = epic ? treePhaseBadge(epic.labels) : '';
    const epicPriority = epic ? priorityBadge(epic.priority) : '';
    // Status removed — page is hard-filtered to open beads
    const allChildren = allChildrenByEpic.get(epicId) || group.children;
    const progressHtml = treeProgressBar(allChildren, _allBeads);
    const childCount = group.children.length;
    const allCount = allChildren.length;
    const countLabel = hasFilters && childCount !== allCount
      ? `${childCount}/${allCount}`
      : `${allCount}`;

    html += `
      <details ${openAttr} class="tree-group mb-2">
        <summary class="cursor-pointer py-2 px-3 bg-gray-800 rounded-lg border border-gray-700 hover:border-gray-600 list-none flex items-center gap-2"
                 onclick="event.target.closest('.tree-group') && event.target.closest('.tree-group').querySelector('.tree-epic-link')?.blur()">
          <span class="tree-chevron text-gray-500 text-xs transition-transform flex-shrink-0">&#9654;</span>
          <span class="flex-shrink-0">📦</span>
          <span class="font-semibold text-sm truncate tree-epic-link cursor-pointer hover:text-indigo-400"
                onclick="event.stopPropagation(); navigateTo('/bead/${epicId}')">${epicTitle}</span>
          <span class="font-mono text-xs text-gray-600">${epicId}</span>
          ${epicPriority}
          ${epicPhase}
          <span class="text-xs text-gray-500">(${countLabel})</span>
          ${progressHtml}
        </summary>
        <div class="ml-4 mt-1 border-l-2 border-gray-700 pl-3">
          ${childCount ? group.children.map(i => renderTreeNode(i, terms, query)).join('') : '<div class="text-xs text-gray-600 italic py-2 pl-2">No matching children</div>'}
        </div>
      </details>`;
  }

  // Orphans (no epic parent)
  if (orphans.length) {
    html += `
      <details ${openAttr} class="tree-group mb-2 mt-4">
        <summary class="cursor-pointer py-2 px-3 bg-gray-800/50 rounded-lg border border-dashed border-gray-700 hover:border-gray-600 list-none flex items-center gap-2">
          <span class="tree-chevron text-gray-500 text-xs transition-transform flex-shrink-0">&#9654;</span>
          <span class="font-semibold text-sm text-gray-400">Ungrouped</span>
          <span class="text-xs text-gray-500">(${orphans.length})</span>
        </summary>
        <div class="ml-4 mt-1 border-l-2 border-gray-700/50 pl-3">
          ${orphans.map(i => renderTreeNode(i, terms, query)).join('')}
        </div>
      </details>`;
  }

  return html || '<div class="text-gray-500 text-center py-8">No beads to display in tree view</div>';
}

window.treeToggleAll = function() {
  _treeExpanded = !_treeExpanded;
  const btn = document.getElementById('tree-toggle-btn');
  if (btn) btn.textContent = _treeExpanded ? 'Collapse all' : 'Expand all';
  document.querySelectorAll('.tree-group').forEach(d => {
    d.open = _treeExpanded;
  });
};

// ── Deps View (DAG visualization) ──────────────────────────

let _depsShowMode = 'dag'; // 'dag' or 'flat'
let _dagZoom = 1;
let _dagPanX = 0;
let _dagPanY = 0;

function _buildDepGraph(filtered, allBeads) {
  // Build full graph from allBeads, then filter visible set
  const beadMap = new Map();
  for (const b of allBeads) beadMap.set(b.id, b);

  const filteredIds = new Set(filtered.map(b => b.id));

  // Collect edges: child depends_on parent (exclude parent-child/epic edges)
  // Forward: blockerOf[id] = [ids that id blocks]
  // Reverse: dependsOn[id] = [ids that id depends on]
  const blockerOf = new Map();
  const dependsOn = new Map();
  const edgeSet = new Set(); // "from->to" dedup

  for (const b of allBeads) {
    for (const d of b.dependencies || []) {
      if (d.type === 'parent-child') continue; // skip epic hierarchy
      const key = `${d.depends_on_id}->${b.id}`;
      if (edgeSet.has(key)) continue;
      edgeSet.add(key);
      if (!blockerOf.has(d.depends_on_id)) blockerOf.set(d.depends_on_id, []);
      blockerOf.get(d.depends_on_id).push(b.id);
      if (!dependsOn.has(b.id)) dependsOn.set(b.id, []);
      dependsOn.get(b.id).push(d.depends_on_id);
    }
  }

  // Find all nodes participating in dependency edges
  const depNodes = new Set();
  for (const [from, tos] of blockerOf) {
    depNodes.add(from);
    for (const to of tos) depNodes.add(to);
  }

  // Only include nodes in the filtered set (or their immediate deps if connected)
  // Strategy: include any depNode that is in filtered, plus its connected neighbours
  const visibleNodes = new Set();
  for (const id of depNodes) {
    if (filteredIds.has(id)) {
      visibleNodes.add(id);
      // Include immediate connections
      for (const dep of (dependsOn.get(id) || [])) visibleNodes.add(dep);
      for (const blocked of (blockerOf.get(id) || [])) visibleNodes.add(blocked);
    }
  }

  // Assign layers via longest-path from roots (nodes with no dependencies)
  const layers = new Map(); // id -> layer number
  const visited = new Set();
  function assignLayer(id, depth) {
    if (visited.has(id) && (layers.get(id) || 0) >= depth) return;
    visited.add(id);
    layers.set(id, Math.max(layers.get(id) || 0, depth));
    for (const child of (blockerOf.get(id) || [])) {
      if (visibleNodes.has(child)) assignLayer(child, depth + 1);
    }
  }
  // Roots: visible nodes with no dependencies (or deps outside visible set)
  for (const id of visibleNodes) {
    const deps = (dependsOn.get(id) || []).filter(d => visibleNodes.has(d));
    if (deps.length === 0) assignLayer(id, 0);
  }
  // Handle cycles: assign unvisited nodes to layer 0
  for (const id of visibleNodes) {
    if (!visited.has(id)) layers.set(id, 0);
  }

  // Group by layer
  const maxLayer = Math.max(0, ...layers.values());
  const layerGroups = [];
  for (let i = 0; i <= maxLayer; i++) layerGroups.push([]);
  for (const id of visibleNodes) {
    const l = layers.get(id) || 0;
    layerGroups[l].push(id);
  }

  // Sort each layer by priority (lower = higher priority)
  for (const group of layerGroups) {
    group.sort((a, b) => {
      const ba = beadMap.get(a), bb = beadMap.get(b);
      return (ba?.priority ?? 4) - (bb?.priority ?? 4);
    });
  }

  // Find critical path: longest chain of open blockers
  const criticalPath = new Set();
  function findCritical(id) {
    const bead = beadMap.get(id);
    if (!bead || bead.status === 'closed') return 0;
    let maxLen = 0;
    let maxChild = null;
    for (const child of (blockerOf.get(id) || [])) {
      if (!visibleNodes.has(child)) continue;
      const childBead = beadMap.get(child);
      if (childBead && childBead.status !== 'closed') {
        const len = findCritical(child);
        if (len > maxLen) { maxLen = len; maxChild = child; }
      }
    }
    if (maxChild !== null) criticalPath.add(id);
    return maxLen + 1;
  }
  // Start critical path from roots
  let longestStart = null, longestLen = 0;
  for (const group of [layerGroups[0] || []]) {
    for (const id of group) {
      const len = findCritical(id);
      if (len > longestLen) { longestLen = len; longestStart = id; }
    }
  }
  if (longestStart) criticalPath.add(longestStart);

  // Edges for visible nodes only
  const visibleEdges = [];
  for (const id of visibleNodes) {
    for (const dep of (dependsOn.get(id) || [])) {
      if (visibleNodes.has(dep)) {
        visibleEdges.push({ from: dep, to: id });
      }
    }
  }

  // Isolated filtered beads (not in any dep chain)
  const isolated = filtered.filter(b => !depNodes.has(b.id));

  return { beadMap, layerGroups, visibleNodes, visibleEdges, criticalPath, dependsOn, blockerOf, isolated };
}

function _dagNodeStatus(bead, dependsOn, beadMap) {
  if (!bead) return 'unknown';
  if (bead.status === 'closed') return 'closed';
  if (bead.status === 'in_progress') return 'active';
  // Check if blocked (has open dependencies)
  const deps = dependsOn.get(bead.id) || [];
  for (const depId of deps) {
    const dep = beadMap.get(depId);
    if (dep && dep.status !== 'closed') return 'blocked';
  }
  // Open with all deps closed = ready
  if (bead.status === 'open') return 'ready';
  return 'open';
}

function renderDepsView(filtered, terms, query) {
  if (!filtered.length) {
    return '<div class="text-gray-500 text-center py-8">No beads to display</div>';
  }

  const graph = _buildDepGraph(filtered, _allBeads);
  const { beadMap, layerGroups, visibleNodes, visibleEdges, criticalPath, dependsOn, blockerOf, isolated } = graph;

  // Stats
  const blockedCount = [...visibleNodes].filter(id => _dagNodeStatus(beadMap.get(id), dependsOn, beadMap) === 'blocked').length;
  const readyCount = [...visibleNodes].filter(id => _dagNodeStatus(beadMap.get(id), dependsOn, beadMap) === 'ready').length;
  const activeCount = [...visibleNodes].filter(id => _dagNodeStatus(beadMap.get(id), dependsOn, beadMap) === 'active').length;

  // Mode toggle + stats bar
  let html = `
    <div class="flex items-center gap-4 mb-4 flex-wrap">
      <div class="flex items-center gap-2">
        <button onclick="dagToggleMode('dag')" class="text-xs px-2 py-1 rounded ${_depsShowMode === 'dag' ? 'bg-indigo-600 text-white' : 'bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700'}">DAG</button>
        <button onclick="dagToggleMode('flat')" class="text-xs px-2 py-1 rounded ${_depsShowMode === 'flat' ? 'bg-indigo-600 text-white' : 'bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700'}">Flat</button>
      </div>
      <div class="flex items-center gap-3 text-xs">
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-green-500"></span> Ready: ${readyCount}</span>
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-red-500"></span> Blocked: ${blockedCount}</span>
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-purple-500"></span> Active: ${activeCount}</span>
        <span class="text-gray-500">${visibleNodes.size} in graph, ${isolated.length} independent</span>
      </div>
      ${_depsShowMode === 'dag' ? `
      <div class="flex items-center gap-1 ml-auto">
        <button onclick="dagZoom(-0.1)" class="text-xs px-2 py-1 bg-gray-800 border border-gray-700 rounded hover:bg-gray-700 text-gray-300">-</button>
        <span class="text-xs text-gray-500 w-10 text-center">${Math.round(_dagZoom * 100)}%</span>
        <button onclick="dagZoom(0.1)" class="text-xs px-2 py-1 bg-gray-800 border border-gray-700 rounded hover:bg-gray-700 text-gray-300">+</button>
        <button onclick="dagResetView()" class="text-xs px-2 py-1 bg-gray-800 border border-gray-700 rounded hover:bg-gray-700 text-gray-300 ml-1">Reset</button>
      </div>` : ''}
    </div>`;

  if (_depsShowMode === 'flat') {
    html += _renderDepsFlatView(filtered, terms, query, graph);
  } else {
    html += _renderDepsDAGView(filtered, terms, query, graph);
  }

  return html;
}

function _renderDepsFlatView(filtered, terms, query, graph) {
  const { beadMap, dependsOn, blockerOf } = graph;
  const withDeps = filtered.filter(i => i.dependencies?.some(d => d.type !== 'parent-child'));
  const noDeps = filtered.filter(i => !i.dependencies?.some(d => d.type !== 'parent-child'));

  function renderDepRow(issue) {
    const deps = (issue.dependencies || []).filter(d => d.type !== 'parent-child');
    const type = treeTypeIcon(issue.issue_type);
    const titleHtml = highlightText(issue.title || '', terms);
    const nodeStatus = _dagNodeStatus(issue, dependsOn, beadMap);
    const borderCls = nodeStatus === 'blocked' ? 'border-red-600' : nodeStatus === 'ready' ? 'border-green-600' : nodeStatus === 'active' ? 'border-purple-600' : 'border-gray-700';

    const depLinks = deps.map(d => {
      const depBead = beadMap.get(d.depends_on_id);
      const depTitle = depBead ? depBead.title : d.depends_on_id;
      const depStatus = depBead ? depBead.status : 'unknown';
      const statusCls = depStatus === 'closed' ? 'text-green-400' : depStatus === 'in_progress' ? 'text-purple-400' : 'text-yellow-400';
      return `<span class="inline-flex items-center gap-1 text-xs">
        <span class="text-gray-500">depends on</span>
        <a href="/bead/${d.depends_on_id}" onclick="event.stopPropagation(); event.preventDefault(); navigateTo('/bead/${d.depends_on_id}')"
           class="${statusCls} hover:underline font-mono">${d.depends_on_id}</a>
        <span class="text-gray-500 truncate max-w-[150px]" title="${(depTitle || '').replace(/"/g, '&quot;')}">${depTitle || ''}</span>
      </span>`;
    }).join('');

    // Show what this bead blocks
    const blocks = (blockerOf.get(issue.id) || []).map(childId => {
      const child = beadMap.get(childId);
      const childTitle = child ? child.title : childId;
      const childStatus = child ? child.status : 'unknown';
      const statusCls = childStatus === 'closed' ? 'text-green-400' : childStatus === 'in_progress' ? 'text-purple-400' : 'text-yellow-400';
      return `<span class="inline-flex items-center gap-1 text-xs">
        <span class="text-gray-500">blocks</span>
        <a href="/bead/${childId}" onclick="event.stopPropagation(); event.preventDefault(); navigateTo('/bead/${childId}')"
           class="${statusCls} hover:underline font-mono">${childId}</a>
        <span class="text-gray-500 truncate max-w-[150px]" title="${(childTitle || '').replace(/"/g, '&quot;')}">${childTitle || ''}</span>
      </span>`;
    });

    return `
      <div class="p-3 bg-gray-800 rounded-lg border-2 ${borderCls} hover:brightness-110 cursor-pointer transition-all"
           onclick="navigateTo('/bead/${issue.id}')">
        <div class="flex items-center gap-2 mb-2">
          <span>${type}</span>
          <span class="text-sm">${titleHtml}</span>
          <span class="font-mono text-xs text-gray-500">${issue.id}</span>
          ${priorityBadge(issue.priority)}
        </div>
        ${depLinks ? `<div class="flex items-center gap-3 flex-wrap mb-1">${depLinks}</div>` : ''}
        ${blocks.length ? `<div class="flex items-center gap-3 flex-wrap">${blocks.join('')}</div>` : ''}
      </div>`;
  }

  let html = '';
  if (withDeps.length) {
    html += `
      <details open class="mb-6">
        <summary class="text-lg font-semibold mb-3 cursor-pointer">With Dependencies <span class="text-gray-500">(${withDeps.length})</span></summary>
        <div class="space-y-2">${withDeps.map(renderDepRow).join('')}</div>
      </details>`;
  }
  if (noDeps.length) {
    html += `
      <details class="mb-6">
        <summary class="text-lg font-semibold mb-3 cursor-pointer">No Dependencies <span class="text-gray-500">(${noDeps.length})</span></summary>
        <div class="space-y-2">${noDeps.map(i => renderIssueRow(i, terms, query)).join('')}</div>
      </details>`;
  }
  return html || '<div class="text-gray-500 text-center py-8">No beads to display</div>';
}

function _renderDepsDAGView(filtered, terms, query, graph) {
  const { beadMap, layerGroups, visibleNodes, visibleEdges, criticalPath, dependsOn, blockerOf, isolated } = graph;

  if (visibleNodes.size === 0 && isolated.length === 0) {
    return '<div class="text-gray-500 text-center py-8">No dependency relationships found</div>';
  }

  // Render layered DAG
  // Each layer is a column (left to right: blockers -> blocked)
  // Nodes are positioned in a grid; SVG overlay draws edges

  const NODE_W = 220;
  const NODE_H = 64;
  const LAYER_GAP = 80;
  const NODE_GAP = 16;

  // Compute positions
  const nodePos = new Map(); // id -> { x, y, layer, idx }
  let maxY = 0;

  for (let l = 0; l < layerGroups.length; l++) {
    const group = layerGroups[l];
    const x = l * (NODE_W + LAYER_GAP);
    for (let i = 0; i < group.length; i++) {
      const y = i * (NODE_H + NODE_GAP);
      nodePos.set(group[i], { x, y, layer: l, idx: i });
      if (y + NODE_H > maxY) maxY = y + NODE_H;
    }
  }

  const totalW = layerGroups.length * (NODE_W + LAYER_GAP) - LAYER_GAP;
  const totalH = maxY;

  // Build SVG edges
  let svgEdges = '';
  for (const edge of visibleEdges) {
    const fromPos = nodePos.get(edge.from);
    const toPos = nodePos.get(edge.to);
    if (!fromPos || !toPos) continue;

    const x1 = fromPos.x + NODE_W;
    const y1 = fromPos.y + NODE_H / 2;
    const x2 = toPos.x;
    const y2 = toPos.y + NODE_H / 2;

    const isCritical = criticalPath.has(edge.from) && criticalPath.has(edge.to);
    const fromBead = beadMap.get(edge.from);
    const toBead = beadMap.get(edge.to);
    const isResolved = fromBead?.status === 'closed';

    let strokeColor = '#4b5563'; // gray
    let strokeWidth = 1.5;
    let dashArray = '';
    if (isCritical) { strokeColor = '#ef4444'; strokeWidth = 2.5; }
    else if (isResolved) { strokeColor = '#22c55e'; dashArray = 'stroke-dasharray="4 3"'; }

    // Bezier curve for the edge
    const midX = (x1 + x2) / 2;
    svgEdges += `<path d="M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}"
      fill="none" stroke="${strokeColor}" stroke-width="${strokeWidth}" ${dashArray}
      marker-end="url(#arrowhead${isCritical ? '-critical' : isResolved ? '-resolved' : ''})"/>`;
  }

  // Build node elements
  let nodeHtml = '';
  for (const [id, pos] of nodePos) {
    const bead = beadMap.get(id);
    if (!bead) continue;
    const nodeStatus = _dagNodeStatus(bead, dependsOn, beadMap);
    const title = highlightText(bead.title || '', terms);
    const icon = treeTypeIcon(bead.issue_type);

    let borderCls, bgCls;
    switch (nodeStatus) {
      case 'blocked': borderCls = 'border-red-500'; bgCls = 'bg-red-950'; break;
      case 'ready': borderCls = 'border-green-500'; bgCls = 'bg-green-950'; break;
      case 'active': borderCls = 'border-purple-500'; bgCls = 'bg-purple-950'; break;
      case 'closed': borderCls = 'border-gray-600'; bgCls = 'bg-gray-800 opacity-60'; break;
      default: borderCls = 'border-gray-600'; bgCls = 'bg-gray-800'; break;
    }
    const critCls = criticalPath.has(id) && nodeStatus !== 'closed' ? 'dag-node-critical' : '';

    nodeHtml += `
      <div class="dag-node absolute rounded-lg border-2 ${borderCls} ${bgCls} ${critCls} p-2 cursor-pointer hover:brightness-125 transition-all overflow-hidden"
           style="left:${pos.x}px; top:${pos.y}px; width:${NODE_W}px; height:${NODE_H}px;"
           onclick="navigateTo('/bead/${id}')"
           title="${(bead.title || '').replace(/"/g, '&quot;')}">
        <div class="flex items-center gap-1.5 mb-1">
          <span class="text-xs flex-shrink-0">${icon}</span>
          <span class="text-xs font-medium truncate flex-1">${title}</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="font-mono text-[10px] text-gray-400">${id}</span>
          ${priorityBadge(bead.priority)}
        </div>
      </div>`;
  }

  // Layer labels
  let layerLabels = '';
  for (let l = 0; l < layerGroups.length; l++) {
    if (!layerGroups[l].length) continue;
    const x = l * (NODE_W + LAYER_GAP);
    const label = l === 0 ? 'Roots (no blockers)' : `Layer ${l}`;
    layerLabels += `<div class="absolute text-[10px] text-gray-500 font-medium" style="left:${x}px; top:-20px;">${label}</div>`;
  }

  // Legend
  const legend = `
    <div class="flex items-center gap-4 text-[10px] text-gray-400 mt-2 flex-wrap">
      <span class="flex items-center gap-1"><span class="w-3 h-1.5 bg-red-500 rounded-sm"></span> Critical path</span>
      <span class="flex items-center gap-1"><span class="w-3 h-1.5 bg-gray-500 rounded-sm"></span> Dependency</span>
      <span class="flex items-center gap-1"><span class="w-3 h-0.5 border-t border-dashed border-green-500" style="width:12px"></span> Resolved</span>
      <span class="flex items-center gap-1"><span class="w-3 h-3 border-2 border-green-500 rounded-sm"></span> Ready</span>
      <span class="flex items-center gap-1"><span class="w-3 h-3 border-2 border-red-500 rounded-sm"></span> Blocked</span>
      <span class="flex items-center gap-1"><span class="w-3 h-3 border-2 border-purple-500 rounded-sm"></span> In progress</span>
    </div>`;

  let html = `
    <div class="dag-viewport overflow-auto border border-gray-700 rounded-lg bg-gray-900/50 relative" id="dag-viewport"
         style="max-height: calc(100vh - 18rem);">
      <div class="dag-canvas relative" id="dag-canvas"
           style="transform: scale(${_dagZoom}) translate(${_dagPanX}px, ${_dagPanY}px); transform-origin: 0 0;
                  width: ${totalW + 40}px; height: ${totalH + 40}px; padding: 30px 20px 20px 20px;">
        ${layerLabels}
        <svg class="absolute inset-0" style="width:${totalW + 40}px; height:${totalH + 40}px; padding: 30px 20px 20px 20px; pointer-events:none;">
          <defs>
            <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <polygon points="0 0, 8 3, 0 6" fill="#4b5563"/>
            </marker>
            <marker id="arrowhead-critical" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <polygon points="0 0, 8 3, 0 6" fill="#ef4444"/>
            </marker>
            <marker id="arrowhead-resolved" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <polygon points="0 0, 8 3, 0 6" fill="#22c55e"/>
            </marker>
          </defs>
          ${svgEdges}
        </svg>
        ${nodeHtml}
      </div>
    </div>
    ${legend}`;

  // Isolated beads section
  if (isolated.length) {
    html += `
      <details class="mt-4">
        <summary class="text-sm font-semibold mb-2 cursor-pointer text-gray-400">Independent Beads <span class="text-gray-500">(${isolated.length})</span></summary>
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
          ${isolated.map(b => {
            const icon = treeTypeIcon(b.issue_type);
            const titleHtml = highlightText(b.title || '', terms);
            return `<div class="p-2 bg-gray-800 rounded border border-gray-700 hover:border-gray-500 cursor-pointer text-xs"
                         onclick="navigateTo('/bead/${b.id}')">
              <div class="flex items-center gap-1.5">
                <span>${icon}</span>
                <span class="truncate">${titleHtml}</span>
                <span class="font-mono text-gray-500">${b.id}</span>
                ${priorityBadge(b.priority)}
              </div>
            </div>`;
          }).join('')}
        </div>
      </details>`;
  }

  return html;
}

window.dagToggleMode = function(mode) {
  _depsShowMode = mode;
  renderBeadResults(document.getElementById('global-search')?.value?.trim() || '');
};

window.dagZoom = function(delta) {
  _dagZoom = Math.max(0.3, Math.min(2, _dagZoom + delta));
  const canvas = document.getElementById('dag-canvas');
  if (canvas) canvas.style.transform = `scale(${_dagZoom}) translate(${_dagPanX}px, ${_dagPanY}px)`;
  const zoomLabel = document.querySelector('.dag-viewport ~ div .text-xs.text-gray-500.w-10, .flex .text-xs.text-gray-500.w-10');
  // Re-render to update zoom label
  renderBeadResults(document.getElementById('global-search')?.value?.trim() || '');
};

window.dagResetView = function() {
  _dagZoom = 1;
  _dagPanX = 0;
  _dagPanY = 0;
  renderBeadResults(document.getElementById('global-search')?.value?.trim() || '');
};

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
let timelineInterval = null;

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
    const [status, allBeads, approvedData] = await Promise.all([
      api('/api/dispatch/status'),
      api('/api/beads/list'),
      api('/api/dispatch/approved'),
    ]);
    const beadList = Array.isArray(allBeads) ? allBeads : [];
    const waitingBeads = Array.isArray(approvedData?.waiting) ? approvedData.waiting : [];
    const blockedBeads = Array.isArray(approvedData?.blocked) ? approvedData.blocked : [];

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

    // Approved — Waiting for Dispatch (unblocked)
    html += `<div class="mb-8">
      <h2 class="text-lg font-semibold mb-3 text-blue-400">Approved — Waiting for Dispatch</h2>`;
    if (waitingBeads.length > 0) {
      for (const b of waitingBeads) {
        html += `
          <a href="/bead/${b.id}" class="block p-4 sm:p-3 bg-gray-800 rounded-lg mb-2 border-l-4 border-blue-500 hover:bg-gray-750">
            <div class="truncate text-sm sm:text-base">${b.title}</div>
            <div class="flex gap-2 items-center flex-wrap mt-1">
              <span class="font-mono text-xs text-gray-400">${b.id}</span>
              ${priorityBadge(b.priority)}
            </div>
          </a>`;
      }
    } else {
      html += `<div class="text-gray-500 text-sm">No unblocked beads waiting</div>`;
    }
    html += `</div>`;

    // Approved — Blocked (has open dependencies)
    html += `<div class="mb-8">
      <h2 class="text-lg font-semibold mb-3 text-yellow-400">Approved — Blocked</h2>`;
    if (blockedBeads.length > 0) {
      for (const b of blockedBeads) {
        const blockerLinks = (b.blockers || []).map(bl =>
          `<a href="/bead/${bl.id}" onclick="event.stopPropagation()" class="inline-flex items-center gap-1 px-2 py-0.5 bg-yellow-900/50 text-yellow-300 text-xs rounded hover:bg-yellow-800/60">
            <span class="font-mono">${bl.id}</span>
            <span class="text-yellow-400/70">${bl.title}</span>
          </a>`
        ).join('');
        html += `
          <a href="/bead/${b.id}" class="block p-4 sm:p-3 bg-gray-800 rounded-lg mb-2 border-l-4 border-yellow-500 hover:bg-gray-750">
            <div class="truncate text-sm sm:text-base">${b.title}</div>
            <div class="flex gap-2 items-center flex-wrap mt-1">
              <span class="font-mono text-xs text-gray-400">${b.id}</span>
              ${priorityBadge(b.priority)}
            </div>
            <div class="mt-2 flex items-center gap-1 flex-wrap">
              <span class="text-xs text-yellow-500">Blocked by:</span>
              ${blockerLinks}
            </div>
          </a>`;
      }
    } else {
      html += `<div class="text-gray-500 text-sm">No blocked beads</div>`;
    }
    html += `</div>`;

    content.innerHTML = html;

    // Load snippets + token counts for active dispatches (after DOM is ready)
    _loadDispatchSnippets(active, runsByBead);
  }

  await refresh();
  dispatchInterval = setInterval(refresh, 5000); // auto-refresh every 5s
}

// ── Timeline Page ────────────────────────────────────────────

function _timelineStarRating(score, max = 5) {
  if (score == null) return '<span class="text-xs text-gray-600">--</span>';
  const filled = Math.round(score);
  let html = '';
  for (let i = 1; i <= max; i++) {
    html += i <= filled
      ? '<span class="text-amber-400">&#9733;</span>'
      : '<span class="text-gray-600">&#9733;</span>';
  }
  return html;
}

function _timelineDuration(secs) {
  if (secs == null) return '--';
  if (secs < 60) return Math.round(secs) + 's';
  if (secs < 3600) return Math.round(secs / 60) + 'm';
  const h = Math.floor(secs / 3600);
  const m = Math.round((secs % 3600) / 60);
  return h + 'h ' + m + 'm';
}

function _timelineOutcomeBadge(status) {
  if (status === 'DONE') return '<span class="px-2 py-0.5 bg-green-900 text-green-300 text-xs rounded-full font-semibold">DONE</span>';
  if (status === 'BLOCKED') return '<span class="px-2 py-0.5 bg-amber-900 text-amber-300 text-xs rounded-full font-semibold">BLOCKED</span>';
  if (status === 'FAILED') return '<span class="px-2 py-0.5 bg-red-900 text-red-300 text-xs rounded-full font-semibold">FAILED</span>';
  return '<span class="px-2 py-0.5 bg-gray-700 text-gray-400 text-xs rounded-full font-semibold">' + (status || '?') + '</span>';
}

function _timelineBreakdownBar(tb) {
  if (!tb) return '';
  const segments = [
    { pct: tb.research_pct || 0, color: 'bg-indigo-500', label: 'Research' },
    { pct: tb.coding_pct || 0, color: 'bg-green-500', label: 'Coding' },
    { pct: tb.debugging_pct || 0, color: 'bg-amber-500', label: 'Debug' },
    { pct: tb.tooling_workaround_pct || 0, color: 'bg-red-500', label: 'Tooling' },
  ].filter(s => s.pct > 0);
  if (segments.length === 0) return '';
  let bar = '<div class="flex h-2 rounded-full overflow-hidden mt-2 mb-1" title="Time breakdown">';
  for (const s of segments) {
    bar += `<div class="${s.color}" style="width:${s.pct}%" title="${s.label} ${s.pct}%"></div>`;
  }
  bar += '</div>';
  bar += '<div class="flex gap-3 text-xs text-gray-500">';
  for (const s of segments) {
    const dot = s.color.replace('bg-', 'text-');
    bar += `<span><span class="${dot}">&#9679;</span> ${s.label} ${s.pct}%</span>`;
  }
  bar += '</div>';
  return bar;
}

function _timelineTypeIcon(status) {
  if (status === 'DONE') return '<span class="text-green-400">&#10003;</span>';
  if (status === 'BLOCKED') return '<span class="text-amber-400">&#9888;</span>';
  if (status === 'FAILED') return '<span class="text-red-400">&#10007;</span>';
  return '<span class="text-gray-500">&#9679;</span>';
}

async function renderTimeline() {
  pageTitle.textContent = 'Timeline';
  if (timelineInterval) clearInterval(timelineInterval);

  let currentRange = '1d';

  function rangeToParam(r) {
    if (r === '1D') return '1d';
    if (r === '1W') return '7d';
    if (r === '1M') return '30d';
    if (r === 'All') return '';
    return '1d';
  }

  async function refresh() {
    const rangeParam = rangeToParam(currentRange);
    let statsUrl = '/api/timeline/stats';
    let feedUrl = '/api/timeline';
    const qs = rangeParam ? '?range=' + rangeParam : '';
    statsUrl += qs;
    feedUrl += qs;

    const [stats, entries] = await Promise.all([api(statsUrl), api(feedUrl)]);

    let html = '';

    // Timeframe toggle
    html += '<div class="flex items-center gap-2 mb-6">';
    html += '<div class="inline-flex rounded-lg bg-gray-800 border border-gray-700 p-0.5">';
    for (const r of ['1D', '1W', '1M', 'All']) {
      const active = r === currentRange;
      const cls = active
        ? 'bg-indigo-600 text-white'
        : 'text-gray-400 hover:text-gray-200 hover:bg-gray-700';
      html += `<button onclick="window._timelineSetRange('${r}')" class="px-3 py-1 text-sm font-medium rounded-md transition-colors ${cls}">${r}</button>`;
    }
    html += '</div>';
    html += '</div>';

    // Stats tiles
    html += '<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-6">';

    // Completed + success rate
    const successPct = stats.success_rate != null ? (stats.success_rate * 100).toFixed(0) + '%' : '--';
    html += `<div class="p-4 bg-gray-800 rounded-lg border border-gray-700">
      <div class="text-xs text-gray-500 mb-1">Completed</div>
      <div class="text-2xl font-bold text-green-400">${stats.completed_count || 0}</div>
      <div class="text-xs text-gray-500 mt-1">${successPct} success</div>
    </div>`;

    // Failed + Blocked
    const failBlocked = (stats.failed_count || 0) + (stats.blocked_count || 0);
    html += `<div class="p-4 bg-gray-800 rounded-lg border border-gray-700">
      <div class="text-xs text-gray-500 mb-1">Failed / Blocked</div>
      <div class="text-2xl font-bold text-red-400">${failBlocked}</div>
      <div class="text-xs text-gray-500 mt-1">${stats.failed_count || 0} failed, ${stats.blocked_count || 0} blocked</div>
    </div>`;

    // Avg Duration
    html += `<div class="p-4 bg-gray-800 rounded-lg border border-gray-700">
      <div class="text-xs text-gray-500 mb-1">Avg Duration</div>
      <div class="text-2xl font-bold text-indigo-400">${_timelineDuration(stats.avg_duration)}</div>
    </div>`;

    // Avg Tooling Score
    html += `<div class="p-4 bg-gray-800 rounded-lg border border-gray-700">
      <div class="text-xs text-gray-500 mb-1">Avg Tooling</div>
      <div class="text-lg mt-1">${_timelineStarRating(stats.avg_tooling_score)}</div>
      <div class="text-xs text-gray-500 mt-1">${stats.avg_tooling_score != null ? stats.avg_tooling_score.toFixed(1) + '/5' : '--'}</div>
    </div>`;

    // Avg Confidence Score
    html += `<div class="p-4 bg-gray-800 rounded-lg border border-gray-700">
      <div class="text-xs text-gray-500 mb-1">Avg Confidence</div>
      <div class="text-lg mt-1">${_timelineStarRating(stats.avg_confidence_score)}</div>
      <div class="text-xs text-gray-500 mt-1">${stats.avg_confidence_score != null ? stats.avg_confidence_score.toFixed(1) + '/5' : '--'}</div>
    </div>`;

    html += '</div>';

    // Feed
    html += '<div class="mb-4"><h2 class="text-lg font-semibold text-indigo-400">Feed</h2></div>';

    if (!Array.isArray(entries) || entries.length === 0) {
      html += '<div class="text-gray-500 text-sm">No timeline entries for this period</div>';
    } else {
      html += '<div class="space-y-2">';
      for (const e of entries) {
        const ts = e.completed_at || e.started_at || '';
        const commitBadge = e.commit_hash
          ? `<span class="font-mono text-xs text-gray-400">${e.commit_hash.slice(0, 8)}</span>`
          : '';
        const discovered = e.discovered_beads_count
          ? `<span class="text-xs text-purple-400">+${e.discovered_beads_count} discovered</span>`
          : '';
        const toolStars = _timelineStarRating(e.scores?.tooling);
        const confStars = _timelineStarRating(e.scores?.confidence);
        const statusColor = e.status === 'DONE' ? 'green' : e.status === 'BLOCKED' ? 'amber' : e.status === 'FAILED' ? 'red' : 'gray';
        const detailId = 'tl-detail-' + (e.bead_id || '').replace(/[^a-zA-Z0-9-]/g, '');

        // Collapsed card
        html += `<div class="bg-gray-800 rounded-lg border border-gray-700 border-l-4 border-l-${statusColor}-500">`;
        html += `<div class="p-3 cursor-pointer" onclick="document.getElementById('${detailId}').classList.toggle('hidden')">`;
        html += '<div class="flex items-center gap-2 flex-wrap">';
        html += `<span class="text-xs text-gray-500 whitespace-nowrap">${ts}</span>`;
        html += _timelineTypeIcon(e.status);
        html += `<a href="/dispatch/trace/${e.bead_id}" class="font-mono text-sm text-indigo-400 hover:underline" onclick="event.stopPropagation()">${e.bead_id}</a>`;
        html += _timelineOutcomeBadge(e.status);
        html += `<span class="text-xs text-gray-400">${_timelineDuration(e.duration_secs)}</span>`;
        html += commitBadge;
        html += `<span class="text-xs ml-auto">${toolStars}</span>`;
        html += `<span class="text-xs">${confStars}</span>`;
        html += discovered;
        html += '</div>';
        html += '</div>';

        // Expanded detail (hidden by default)
        html += `<div id="${detailId}" class="hidden border-t border-gray-700 p-3">`;
        if (e.reason) {
          html += `<div class="text-sm text-gray-300 mb-2"><strong class="text-gray-400">Reason:</strong> ${e.reason}</div>`;
        }
        if (e.commit_message) {
          html += `<div class="text-sm text-gray-300 mb-2"><strong class="text-gray-400">Commit:</strong> ${e.commit_message}</div>`;
        }
        if (e.lines_added != null || e.lines_removed != null) {
          html += `<div class="text-xs text-gray-500 mb-2">`;
          if (e.lines_added != null) html += `<span class="text-green-400">+${e.lines_added}</span> `;
          if (e.lines_removed != null) html += `<span class="text-red-400">-${e.lines_removed}</span> `;
          if (e.files_changed != null) html += `<span class="text-gray-400">(${e.files_changed} files)</span>`;
          html += '</div>';
        }
        html += _timelineBreakdownBar(e.time_breakdown);
        if (e.scores) {
          html += '<div class="flex gap-4 mt-2 text-xs">';
          html += `<span class="text-gray-500">Tooling: ${_timelineStarRating(e.scores.tooling)}</span>`;
          html += `<span class="text-gray-500">Clarity: ${_timelineStarRating(e.scores.clarity)}</span>`;
          html += `<span class="text-gray-500">Confidence: ${_timelineStarRating(e.scores.confidence)}</span>`;
          html += '</div>';
        }
        if (e.failure_category) {
          html += `<div class="text-xs text-red-400 mt-2">Failure: ${e.failure_category}</div>`;
        }
        if (e.discovered_beads_count) {
          html += `<div class="text-xs text-purple-400 mt-2">${e.discovered_beads_count} bead(s) discovered during execution</div>`;
        }
        // Links
        html += '<div class="flex gap-3 mt-3">';
        html += `<a href="/dispatch/trace/${e.bead_id}" class="text-xs text-indigo-400 hover:underline" onclick="event.stopPropagation()">Trace</a>`;
        html += `<a href="/bead/${e.bead_id}" class="text-xs text-gray-400 hover:underline" onclick="event.stopPropagation()">Bead detail</a>`;
        html += '</div>';
        html += '</div>';

        html += '</div>';
      }
      html += '</div>';
    }

    content.innerHTML = html;
  }

  window._timelineSetRange = function(r) {
    currentRange = r;
    refresh();
  };

  await refresh();
  timelineInterval = setInterval(refresh, 15000); // slower poll — historical data
}

async function renderTrace(runName) {
  pageTitle.textContent = `Trace: ${runName}`;
  const trace = await api(`/api/dispatch/trace/${runName}`);
  if (trace.error) {
    content.innerHTML = `<div class="text-red-400">${trace.error}</div>`;
    return;
  }

  // Use the resolved run directory name (slug may be a bead ID)
  const resolvedRun = trace.run || runName;

  // If this is a live/running dispatch, show live view instead of trace
  if (trace.is_live) {
    const bead = Array.isArray(trace.bead) ? trace.bead[0] : trace.bead;
    content.innerHTML = `
      <div class="mb-6">
        <a href="/dispatch" class="text-indigo-400 text-sm hover:underline mb-2 block">← Back to Dispatch</a>
        <h1 class="text-2xl font-bold mb-2">${bead?.title || trace.bead_id}</h1>
        <div class="flex gap-2 items-center mb-4">
          <a href="/bead/${trace.bead_id}" class="font-mono text-sm text-indigo-400 hover:underline">${trace.bead_id}</a>
          ${bead ? priorityBadge(bead.priority) : ''}
          <span class="px-2 py-0.5 bg-green-900 text-green-300 text-xs rounded font-mono animate-pulse">RUNNING</span>
        </div>
      </div>
      <div style="height: 20rem;"></div>`;
    showLivePanel(resolvedRun);
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
      <button onclick="showCompletedPanel('${resolvedRun}')"
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
      <span class="text-xs text-gray-600 self-center" title="Hold Shift to select text, Ctrl+Shift+V to paste">Shift+drag to select</span>
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
    scrollback: 10000,
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
  // ClipboardAddon handles OSC 52 sequences from tmux/vim for clipboard sync
  if (typeof ClipboardAddon !== 'undefined') {
    term.loadAddon(new ClipboardAddon.ClipboardAddon());
  }
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
  // tmux mouse is ON so scroll wheel works (triggers tmux copy-mode).
  // Hold Shift to select text at browser level (bypasses tmux mouse capture).
  // ClipboardAddon handles OSC 52 from tmux/vim yank → browser clipboard.
  // onSelectionChange copies Shift-selected text to clipboard automatically.

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

  // OSC 52 clipboard sync is now handled by ClipboardAddon above.

  // ── Paste Handling ──────────────────────────────────────
  // Multiple paths to ensure paste works:
  // 1. xterm.js onPaste (browser paste event — Ctrl+V, middle-click)
  // 2. Ctrl+Shift+V keyboard shortcut
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

  // 2. Ctrl+Shift+V paste shortcut (explicit fallback)
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
  if (timelineInterval) { clearInterval(timelineInterval); timelineInterval = null; }

  // Remove bead search listener when leaving beads page
  globalSearch.removeEventListener('input', _beadsSearchHandler);

  // Update active nav
  document.querySelectorAll('.nav-link').forEach(el => {
    el.classList.toggle('active', path.startsWith('/' + el.dataset.page));
  });

  // Update global search placeholder based on page
  globalSearch.placeholder = (path === '/' || path === '/beads') ? 'Search beads...' : 'Search graph...';

  if (path === '/' || path === '/beads') {
    renderBeads();
  } else if (path.startsWith('/dispatch/trace/')) {
    renderTrace(path.split('/dispatch/trace/')[1]);
  } else if (path === '/dispatch') {
    renderDispatch();
  } else if (path.startsWith('/bead/')) {
    renderBeadDetail(path.split('/bead/')[1]);
  } else if (path === '/timeline') {
    renderTimeline();
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

// Global search — context-sensitive (beads page searches beads, others search graph)
globalSearch.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    const q = globalSearch.value.trim();
    if (q) {
      const path = window.location.pathname;
      if (path === '/' || path === '/beads') {
        // On beads page: trigger bead search directly
        renderBeadResults(q);
      } else {
        navigateTo(`/search?q=${encodeURIComponent(q)}`);
      }
    }
  }
});

// Client-side nav (no full page reload)
document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]');
  if (link && link.origin === window.location.origin) {
    e.preventDefault();
    navigateTo(link.pathname + link.search);
  }
});

window.addEventListener('popstate', route);

// ── Nav Badges ───────────────────────────────────────────────

async function updateNavBadges() {
  try {
    const [ready, allBeads, sessions, terminals, dispatchStatus, timelineStats] = await Promise.all([
      api('/api/beads/ready'),
      api('/api/beads/list'),
      api('/api/active?threshold=600'),
      api('/api/terminals'),
      api('/api/dispatch/status'),
      api('/api/timeline/stats?range=1d'),
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

    // Timeline: today's completed count
    const todayDone = timelineStats?.completed_count || 0;
    document.getElementById('badge-timeline').textContent = todayDone || '';

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

// Initial route
route();
