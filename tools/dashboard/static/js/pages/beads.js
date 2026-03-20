// Beads page Alpine component.
// Registered via alpine:init. Loaded in base.html, initialised by
// renderBeadsFragment() which calls Alpine.initTree() after injecting the HTML.
//
// Handles all four view modes: List, Board, Tree, Deps (DAG + flat).
// Filter state is synced to URL params (replaceState). View preference is
// persisted in localStorage.
//
// Pitfall: Alpine does NOT make Map/Set reactive — use plain objects/arrays.
// Pitfall: use $nextTick for post-render DOM work after reactive state changes.

(function () {

  // ── Helpers ─────────────────────────────────────────────────

  function _highlightText(text, terms) {
    if (!terms || !terms.length) return _esc(text);
    const escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    const re = new RegExp(`(${escaped.join('|')})`, 'gi');
    return _esc(text).replace(re, '<mark class="bg-yellow-600 text-white rounded px-0.5">$1</mark>');
  }

  function _esc(s) {
    return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _getPhase(labels) {
    for (const l of labels || []) {
      if (l.startsWith('readiness:')) return l.split(':')[1];
    }
    return null;
  }

  function _getEpicParent(issue) {
    for (const d of issue.dependencies || []) {
      if (d.type === 'parent-child') return d.depends_on_id;
    }
    return null;
  }

  function _isBlocked(issue) {
    return issue.status === 'open' && (issue.dependencies || []).some(d => d.status !== 'closed');
  }

  function _typeIcon(type) {
    if (type === 'epic') return '📦';
    if (type === 'bug') return '🐛';
    if (type === 'feature') return '✨';
    return '📋';
  }

  function _creatorIcon(created_by) {
    if (!created_by) return '';
    if (created_by.startsWith('librarian:')) return '📖';
    if (created_by.startsWith('terminal:')) return '💻';
    if (created_by.startsWith('dispatch:')) return '🤖';
    return '🧑';
  }

  function _priorityBadgeHtml(p) {
    return `<span class="badge badge-p${p}">P${p}</span>`;
  }

  function _fmtDate(raw) {
    if (!raw) return '';
    const d = new Date(raw);
    return d.getFullYear() + '-' +
      String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0') + ' ' +
      String(d.getHours()).padStart(2, '0') + ':' +
      String(d.getMinutes()).padStart(2, '0');
  }

  function _sortBeads(beads, col, dir) {
    const mult = dir === 'asc' ? 1 : -1;
    const phaseOrder = { approved: 0, specified: 1, draft: 2, idea: 3 };
    const statusOrder = { in_progress: 0, open: 1, blocked: 2, closed: 3 };
    return [...beads].sort((a, b) => {
      let va, vb;
      switch (col) {
        case 'title':
          return mult * (a.title || '').toLowerCase().localeCompare((b.title || '').toLowerCase());
        case 'id':
          return mult * (a.id || '').localeCompare(b.id || '');
        case 'priority':
          return mult * ((a.priority ?? 4) - (b.priority ?? 4));
        case 'phase':
          va = phaseOrder[_getPhase(a.labels)] ?? 4;
          vb = phaseOrder[_getPhase(b.labels)] ?? 4;
          return mult * (va - vb);
        case 'type':
          return mult * (a.issue_type || '').localeCompare(b.issue_type || '');
        case 'creator':
          return mult * (a.created_by || '').localeCompare(b.created_by || '');
        case 'epic':
          va = (_getEpicParent(a) || '').toLowerCase();
          vb = (_getEpicParent(b) || '').toLowerCase();
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
          va = statusOrder[a.status] ?? 4;
          vb = statusOrder[b.status] ?? 4;
          return mult * (va - vb);
        default:
          return 0;
      }
    });
  }

  // ── DAG rendering (imperative — computes SVG HTML for x-html binding) ──

  function _buildDepGraph(filtered, allBeads) {
    const beadMap = {};
    for (const b of allBeads) beadMap[b.id] = b;
    const filteredIds = new Set(filtered.map(b => b.id));

    const blockerOf = {};
    const dependsOn = {};
    const edgeSet = new Set();
    for (const b of allBeads) {
      for (const d of b.dependencies || []) {
        if (d.type === 'parent-child') continue;
        const key = `${d.depends_on_id}->${b.id}`;
        if (edgeSet.has(key)) continue;
        edgeSet.add(key);
        (blockerOf[d.depends_on_id] = blockerOf[d.depends_on_id] || []).push(b.id);
        (dependsOn[b.id] = dependsOn[b.id] || []).push(d.depends_on_id);
      }
    }

    const depNodes = new Set();
    for (const from of Object.keys(blockerOf)) {
      depNodes.add(from);
      for (const to of blockerOf[from]) depNodes.add(to);
    }

    const visibleNodes = new Set();
    for (const id of depNodes) {
      if (filteredIds.has(id)) {
        visibleNodes.add(id);
        for (const dep of (dependsOn[id] || [])) visibleNodes.add(dep);
        for (const bl of (blockerOf[id] || [])) visibleNodes.add(bl);
      }
    }

    // Assign layers (longest path from roots)
    const layers = {};
    const visited = new Set();
    function assignLayer(id, depth) {
      if (visited.has(id) && (layers[id] || 0) >= depth) return;
      visited.add(id);
      layers[id] = Math.max(layers[id] || 0, depth);
      for (const child of (blockerOf[id] || [])) {
        if (visibleNodes.has(child)) assignLayer(child, depth + 1);
      }
    }
    for (const id of visibleNodes) {
      const deps = (dependsOn[id] || []).filter(d => visibleNodes.has(d));
      if (deps.length === 0) assignLayer(id, 0);
    }
    for (const id of visibleNodes) {
      if (!visited.has(id)) layers[id] = 0;
    }

    const maxLayer = visibleNodes.size ? Math.max(0, ...Object.values(layers)) : 0;
    const layerGroups = [];
    for (let i = 0; i <= maxLayer; i++) layerGroups.push([]);
    for (const id of visibleNodes) layerGroups[layers[id] || 0].push(id);
    for (const g of layerGroups) g.sort((a, b) => (beadMap[a]?.priority ?? 4) - (beadMap[b]?.priority ?? 4));

    // Critical path
    const criticalPath = new Set();
    function findCritical(id) {
      const bead = beadMap[id];
      if (!bead || bead.status === 'closed') return 0;
      let maxLen = 0, maxChild = null;
      for (const child of (blockerOf[id] || [])) {
        if (!visibleNodes.has(child)) continue;
        if (beadMap[child]?.status !== 'closed') {
          const len = findCritical(child);
          if (len > maxLen) { maxLen = len; maxChild = child; }
        }
      }
      if (maxChild !== null) criticalPath.add(id);
      return maxLen + 1;
    }
    let longestStart = null, longestLen = 0;
    for (const id of (layerGroups[0] || [])) {
      const len = findCritical(id);
      if (len > longestLen) { longestLen = len; longestStart = id; }
    }
    if (longestStart) criticalPath.add(longestStart);

    const visibleEdges = [];
    for (const id of visibleNodes) {
      for (const dep of (dependsOn[id] || [])) {
        if (visibleNodes.has(dep)) visibleEdges.push({ from: dep, to: id });
      }
    }

    const isolated = filtered.filter(b => !depNodes.has(b.id));
    return { beadMap, layerGroups, visibleNodes, visibleEdges, criticalPath, dependsOn, blockerOf, isolated };
  }

  function _dagNodeStatus(bead, dependsOn, beadMap) {
    if (!bead) return 'unknown';
    if (bead.status === 'closed') return 'closed';
    if (bead.status === 'in_progress') return 'active';
    const deps = dependsOn[bead.id] || [];
    for (const depId of deps) {
      if (beadMap[depId] && beadMap[depId].status !== 'closed') return 'blocked';
    }
    if (bead.status === 'open') return 'ready';
    return 'open';
  }

  function _renderDAGHtml(filtered, terms, allBeads) {
    const graph = _buildDepGraph(filtered, allBeads);
    const { beadMap, layerGroups, visibleNodes, visibleEdges, criticalPath, dependsOn, isolated } = graph;

    if (visibleNodes.size === 0 && isolated.length === 0) {
      return '<div class="text-gray-500 text-center py-8">No dependency relationships found</div>';
    }

    const NODE_W = 220, NODE_H = 64, LAYER_GAP = 80, NODE_GAP = 16;
    const nodePos = {};
    let maxY = 0;
    for (let l = 0; l < layerGroups.length; l++) {
      const x = l * (NODE_W + LAYER_GAP);
      for (let i = 0; i < layerGroups[l].length; i++) {
        const y = i * (NODE_H + NODE_GAP);
        nodePos[layerGroups[l][i]] = { x, y };
        if (y + NODE_H > maxY) maxY = y + NODE_H;
      }
    }
    const totalW = layerGroups.length * (NODE_W + LAYER_GAP) - LAYER_GAP;

    let svgEdges = '';
    for (const edge of visibleEdges) {
      const fp = nodePos[edge.from], tp = nodePos[edge.to];
      if (!fp || !tp) continue;
      const x1 = fp.x + NODE_W, y1 = fp.y + NODE_H / 2;
      const x2 = tp.x, y2 = tp.y + NODE_H / 2;
      const midX = (x1 + x2) / 2;
      const isCritical = criticalPath.has(edge.from) && criticalPath.has(edge.to);
      const isResolved = beadMap[edge.from]?.status === 'closed';
      const stroke = isCritical ? '#ef4444' : isResolved ? '#22c55e' : '#4b5563';
      const sw = isCritical ? 2.5 : 1.5;
      const dash = (!isCritical && isResolved) ? 'stroke-dasharray="4 3"' : '';
      const marker = isCritical ? 'arrowhead-critical' : isResolved ? 'arrowhead-resolved' : 'arrowhead';
      svgEdges += `<path d="M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}" fill="none" stroke="${stroke}" stroke-width="${sw}" ${dash} marker-end="url(#${marker})"/>`;
    }

    let nodeHtml = '';
    for (const [id, pos] of Object.entries(nodePos)) {
      const bead = beadMap[id];
      if (!bead) continue;
      const ns = _dagNodeStatus(bead, dependsOn, beadMap);
      const title = _highlightText(bead.title || '', terms);
      const icon = _typeIcon(bead.issue_type);
      const borderCls = ns === 'blocked' ? 'border-red-500' : ns === 'ready' ? 'border-green-500' : ns === 'active' ? 'border-purple-500' : 'border-gray-600';
      const bgCls = ns === 'blocked' ? 'bg-red-950' : ns === 'ready' ? 'bg-green-950' : ns === 'active' ? 'bg-purple-950' : ns === 'closed' ? 'bg-gray-800 opacity-60' : 'bg-gray-800';
      const critCls = criticalPath.has(id) && ns !== 'closed' ? 'dag-node-critical' : '';
      nodeHtml += `<a href="/bead/${_esc(id)}" data-bead-id="${_esc(id)}"
        class="dag-node absolute rounded-lg border-2 ${borderCls} ${bgCls} ${critCls} p-2 hover:brightness-125 transition-all overflow-hidden"
        style="left:${pos.x}px; top:${pos.y}px; width:${NODE_W}px; height:${NODE_H}px;"
        title="${_esc(bead.title || '')}">
        <div class="flex items-center gap-1.5 mb-1">
          <span class="text-xs flex-shrink-0">${icon}</span>
          <span class="text-xs font-medium truncate flex-1">${title}</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="font-mono text-[10px] text-gray-400">${_esc(id)}</span>
          ${_priorityBadgeHtml(bead.priority)}
        </div>
      </a>`;
    }

    let layerLabels = '';
    for (let l = 0; l < layerGroups.length; l++) {
      if (!layerGroups[l].length) continue;
      layerLabels += `<div class="absolute text-[10px] text-gray-500 font-medium" style="left:${l*(NODE_W+LAYER_GAP)}px; top:-20px;">${l === 0 ? 'Roots (no blockers)' : 'Layer ' + l}</div>`;
    }

    let isolatedHtml = '';
    if (isolated.length) {
      const cards = isolated.map(b => `
        <a href="/bead/${_esc(b.id)}" data-bead-id="${_esc(b.id)}"
           class="p-2 bg-gray-800 rounded border border-gray-700 hover:border-gray-500 text-xs block">
          <div class="flex items-center gap-1.5">
            <span>${_typeIcon(b.issue_type)}</span>
            <span class="truncate">${_highlightText(b.title || '', terms)}</span>
            <span class="font-mono text-gray-500">${_esc(b.id)}</span>
            ${_priorityBadgeHtml(b.priority)}
          </div>
        </a>`).join('');
      isolatedHtml = `
        <details class="mt-4">
          <summary class="text-sm font-semibold mb-2 cursor-pointer text-gray-400">Independent Beads <span class="text-gray-500">(${isolated.length})</span></summary>
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">${cards}</div>
        </details>`;
    }

    return `
      <div class="dag-viewport overflow-auto border border-gray-700 rounded-lg bg-gray-900/50 relative" style="max-height: calc(100vh - 18rem);">
        <div class="dag-canvas relative" id="dag-canvas"
             style="width:${totalW+40}px; height:${maxY+40}px; padding:30px 20px 20px 20px;">
          ${layerLabels}
          <svg class="absolute inset-0" style="width:${totalW+40}px; height:${maxY+40}px; padding:30px 20px 20px 20px; pointer-events:none;">
            <defs>
              <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#4b5563"/></marker>
              <marker id="arrowhead-critical" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#ef4444"/></marker>
              <marker id="arrowhead-resolved" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#22c55e"/></marker>
            </defs>
            ${svgEdges}
          </svg>
          ${nodeHtml}
        </div>
      </div>
      <div class="flex items-center gap-4 text-[10px] text-gray-400 mt-2 flex-wrap">
        <span class="flex items-center gap-1"><span class="w-3 h-1.5 bg-red-500 rounded-sm"></span> Critical path</span>
        <span class="flex items-center gap-1"><span class="w-3 h-1.5 bg-gray-500 rounded-sm"></span> Dependency</span>
        <span class="flex items-center gap-1"><span class="w-3 h-0.5 border-t border-dashed border-green-500" style="width:12px"></span> Resolved</span>
        <span class="flex items-center gap-1"><span class="w-3 h-3 border-2 border-green-500 rounded-sm"></span> Ready</span>
        <span class="flex items-center gap-1"><span class="w-3 h-3 border-2 border-red-500 rounded-sm"></span> Blocked</span>
        <span class="flex items-center gap-1"><span class="w-3 h-3 border-2 border-purple-500 rounded-sm"></span> In progress</span>
      </div>
      ${isolatedHtml}`;
  }

  function _renderDepsFlatHtml(filtered, terms, graph) {
    const { beadMap, dependsOn, blockerOf } = graph;
    const withDeps = filtered.filter(i => (i.dependencies || []).some(d => d.type !== 'parent-child'));
    const noDeps = filtered.filter(i => !(i.dependencies || []).some(d => d.type !== 'parent-child'));

    function depRowHtml(issue) {
      const deps = (issue.dependencies || []).filter(d => d.type !== 'parent-child');
      const ns = _dagNodeStatus(issue, dependsOn, beadMap);
      const borderCls = ns === 'blocked' ? 'border-red-600' : ns === 'ready' ? 'border-green-600' : ns === 'active' ? 'border-purple-600' : 'border-gray-700';
      const depLinks = deps.map(d => {
        const dep = beadMap[d.depends_on_id];
        const sc = dep?.status === 'closed' ? 'text-green-400' : dep?.status === 'in_progress' ? 'text-purple-400' : 'text-yellow-400';
        return `<span class="inline-flex items-center gap-1 text-xs">
          <span class="text-gray-500">depends on</span>
          <a href="/bead/${_esc(d.depends_on_id)}" class="${sc} hover:underline font-mono">${_esc(d.depends_on_id)}</a>
          <span class="text-gray-500 truncate max-w-[150px]" title="${_esc(dep?.title||'')}">${_esc(dep?.title||'')}</span>
        </span>`;
      }).join('');
      const blocks = (blockerOf[issue.id] || []).map(childId => {
        const child = beadMap[childId];
        const sc = child?.status === 'closed' ? 'text-green-400' : child?.status === 'in_progress' ? 'text-purple-400' : 'text-yellow-400';
        return `<span class="inline-flex items-center gap-1 text-xs">
          <span class="text-gray-500">blocks</span>
          <a href="/bead/${_esc(childId)}" class="${sc} hover:underline font-mono">${_esc(childId)}</a>
          <span class="text-gray-500 truncate max-w-[150px]" title="${_esc(child?.title||'')}">${_esc(child?.title||'')}</span>
        </span>`;
      }).join('');
      return `
        <a href="/bead/${_esc(issue.id)}" data-bead-id="${_esc(issue.id)}"
           class="p-3 bg-gray-800 rounded-lg border-2 ${borderCls} hover:brightness-110 transition-all block">
          <div class="flex items-center gap-2 mb-2">
            <span>${_typeIcon(issue.issue_type)}</span>
            <span class="text-sm">${_highlightText(issue.title||'', terms)}</span>
            <span class="font-mono text-xs text-gray-500">${_esc(issue.id)}</span>
            ${_priorityBadgeHtml(issue.priority)}
          </div>
          ${depLinks ? `<div class="flex items-center gap-3 flex-wrap mb-1">${depLinks}</div>` : ''}
          ${blocks ? `<div class="flex items-center gap-3 flex-wrap">${blocks}</div>` : ''}
        </a>`;
    }

    function simpleRowHtml(issue) {
      return `
        <a href="/bead/${_esc(issue.id)}" data-bead-id="${_esc(issue.id)}"
           class="p-3 bg-gray-800 rounded-lg hover:bg-gray-750 border border-gray-700 block">
          <div class="flex items-center gap-2">
            <span>${_typeIcon(issue.issue_type)}</span>
            <span class="text-sm truncate">${_highlightText(issue.title||'', terms)}</span>
            <span class="font-mono text-xs text-gray-500">${_esc(issue.id)}</span>
            ${_priorityBadgeHtml(issue.priority)}
          </div>
        </a>`;
    }

    let html = '';
    if (withDeps.length) {
      html += `<details open class="mb-6"><summary class="text-lg font-semibold mb-3 cursor-pointer">With Dependencies <span class="text-gray-500">(${withDeps.length})</span></summary><div class="space-y-2">${withDeps.map(depRowHtml).join('')}</div></details>`;
    }
    if (noDeps.length) {
      html += `<details class="mb-6"><summary class="text-lg font-semibold mb-3 cursor-pointer">No Dependencies <span class="text-gray-500">(${noDeps.length})</span></summary><div class="space-y-2">${noDeps.map(simpleRowHtml).join('')}</div></details>`;
    }
    return html || '<div class="text-gray-500 text-center py-8">No beads to display</div>';
  }

  // ── Alpine component ─────────────────────────────────────────

  document.addEventListener('alpine:init', () => {
    Alpine.data('beadsPage', () => ({
      allBeads: [],
      loading: true,
      query: '',
      view: localStorage.getItem('beads-view') || 'board',

      // Sort (list view)
      sortCol: 'updated_at',
      sortDir: 'desc',

      // Filters
      fPriority: [],
      fPhase: [],
      fType: [],
      fLabels: [],
      fLabelMode: 'or',
      fEpic: '',
      fBlocked: '',
      fCreator: '',

      // Dropdown open state
      labelDropdownOpen: false,

      // Bulk selection (plain object — Alpine reactivity needs plain obj, not Set)
      selected: {},

      // Tree expand state (plain object keyed by epic id)
      treeOpen: {},
      treeExpandedAll: true,

      // Deps mode
      depsMode: 'dag',

      // Computed deps HTML (set after filter change, rendered via x-html)
      _depsHtml: '',

      _searchHandler: null,

      // ── Lifecycle ──────────────────────────────────────────

      async init() {
        document.getElementById('page-title').textContent = 'Beads';

        // Restore view from URL or localStorage
        const urlView = new URLSearchParams(window.location.search).get('view');
        const validViews = ['list', 'board', 'tree', 'deps'];
        if (urlView && validViews.includes(urlView)) {
          this.view = urlView;
          localStorage.setItem('beads-view', urlView);
        }

        // Restore filters from URL
        this._restoreFromURL();

        // Sync with global search bar
        const gs = document.getElementById('global-search');
        this.query = gs ? gs.value.trim() : '';
        this._searchHandler = () => {
          clearTimeout(this._searchTimer);
          this._searchTimer = setTimeout(() => {
            this.query = document.getElementById('global-search')?.value?.trim() || '';
          }, 300);
        };
        if (gs) gs.addEventListener('input', this._searchHandler);

        // Fetch bead data
        const data = await fetch('/api/beads/list').then(r => r.json());
        this.allBeads = Array.isArray(data) ? data : [];
        this.loading = false;

        // Init tree open state
        this._initTreeOpen();

        // Keep URL in sync
        this.$watch('view', () => this._syncURL());
        this.$watch('fPriority', () => this._syncURL());
        this.$watch('fPhase', () => this._syncURL());
        this.$watch('fType', () => this._syncURL());
        this.$watch('fLabels', () => this._syncURL());
        this.$watch('fLabelMode', () => this._syncURL());
        this.$watch('fEpic', () => this._syncURL());
        this.$watch('fBlocked', () => this._syncURL());
        this.$watch('fCreator', () => this._syncURL());
      },

      destroy() {
        const gs = document.getElementById('global-search');
        if (gs && this._searchHandler) gs.removeEventListener('input', this._searchHandler);
      },

      // ── URL sync ───────────────────────────────────────────

      _restoreFromURL() {
        const p = new URLSearchParams(window.location.search);
        this.fPriority = p.get('priority') ? p.get('priority').split(',').map(Number) : [];
        this.fPhase = p.get('phase') ? p.get('phase').split(',') : [];
        this.fType = p.get('type') ? p.get('type').split(',') : [];
        this.fLabels = p.get('labels') ? p.get('labels').split(',') : [];
        this.fLabelMode = p.get('labelMode') || 'or';
        this.fEpic = p.get('epic') || '';
        this.fBlocked = p.get('blocked') || '';
        this.fCreator = p.get('creator') || '';
      },

      _syncURL() {
        const p = new URLSearchParams();
        if (this.view !== 'board') p.set('view', this.view);
        if (this.fPriority.length) p.set('priority', this.fPriority.join(','));
        if (this.fPhase.length) p.set('phase', this.fPhase.join(','));
        if (this.fType.length) p.set('type', this.fType.join(','));
        if (this.fLabels.length) p.set('labels', this.fLabels.join(','));
        if (this.fLabelMode !== 'or') p.set('labelMode', this.fLabelMode);
        if (this.fEpic) p.set('epic', this.fEpic);
        if (this.fBlocked) p.set('blocked', this.fBlocked);
        if (this.fCreator) p.set('creator', this.fCreator);
        const qs = p.toString();
        history.replaceState({}, '', window.location.pathname + (qs ? '?' + qs : ''));
      },

      // ── Computed props ─────────────────────────────────────

      get hasFilters() {
        return this.fPriority.length || this.fPhase.length || this.fType.length ||
               this.fLabels.length || this.fEpic || this.fBlocked || this.fCreator;
      },

      get filtered() {
        let beads = this.allBeads;
        // Text search
        if (this.query) {
          const terms = this.query.toLowerCase().split(/\s+/).filter(Boolean);
          beads = beads.filter(b => {
            const h = `${b.title||''} ${b.description||''} ${b.id||''}`.toLowerCase();
            return terms.every(t => h.includes(t));
          });
        }
        // Priority
        if (this.fPriority.length) beads = beads.filter(b => this.fPriority.includes(b.priority));
        // Phase
        if (this.fPhase.length) beads = beads.filter(b => {
          const ph = _getPhase(b.labels);
          return ph && this.fPhase.includes(ph);
        });
        // Type
        if (this.fType.length) beads = beads.filter(b => this.fType.includes(b.issue_type));
        // Labels
        if (this.fLabels.length) {
          beads = beads.filter(b => {
            const bl = b.labels || [];
            return this.fLabelMode === 'and'
              ? this.fLabels.every(l => bl.includes(l))
              : this.fLabels.some(l => bl.includes(l));
          });
        }
        // Epic
        if (this.fEpic) {
          beads = beads.filter(b => _getEpicParent(b) === this.fEpic || b.id === this.fEpic);
        }
        // Blocked
        if (this.fBlocked === 'yes') beads = beads.filter(b => _isBlocked(b));
        if (this.fBlocked === 'no') beads = beads.filter(b => !_isBlocked(b));
        // Creator
        if (this.fCreator === 'librarian') beads = beads.filter(b => b.created_by && b.created_by.startsWith('librarian:'));
        return beads;
      },

      get terms() {
        return this.query ? this.query.toLowerCase().split(/\s+/).filter(Boolean) : [];
      },

      get sortedList() {
        return _sortBeads(this.filtered, this.sortCol, this.sortDir);
      },

      get epicTitleMap() {
        const m = {};
        for (const b of this.allBeads) {
          if (b.issue_type === 'epic') m[b.id] = b.title;
        }
        return m;
      },

      get allLabels() {
        const s = new Set();
        for (const b of this.allBeads) {
          for (const l of b.labels || []) {
            if (!l.startsWith('readiness:') && !l.startsWith('dispatch:')) s.add(l);
          }
        }
        return [...s].sort();
      },

      get epics() {
        return this.allBeads.filter(b => b.issue_type === 'epic' && b.status !== 'closed');
      },

      get selectedCount() {
        return Object.keys(this.selected).length;
      },

      get showEmpty() {
        return (this.query || this.hasFilters) && this.filtered.length === 0;
      },

      get showCount() {
        return !!(this.query || this.hasFilters);
      },

      get countText() {
        const n = this.filtered.length;
        return n + ' bead' + (n !== 1 ? 's' : '') +
          (this.query ? ` matching "${this.query}"` : '') +
          (this.hasFilters ? ' (filtered)' : '');
      },

      // ── Board view data ────────────────────────────────────

      get boardColumns() {
        const active = this.filtered.filter(b => b.status !== 'closed');
        const buckets = { idea: [], draft: [], specified: [], approved: [] };
        for (const b of active) {
          const ph = _getPhase(b.labels) || 'idea';
          (buckets[ph] || buckets.idea).push(b);
        }
        return [
          { key: 'idea',      title: 'Ideas',    color: 'border-yellow-500', items: buckets.idea },
          { key: 'draft',     title: 'Drafts',   color: 'border-blue-500',   items: buckets.draft },
          { key: 'specified', title: 'Specified', color: 'border-purple-500', items: buckets.specified },
          { key: 'approved',  title: 'Approved',  color: 'border-green-500',  items: buckets.approved },
        ];
      },

      // ── Tree view data ─────────────────────────────────────

      get treeGroups() {
        const epicMap = {};
        const orphans = [];
        const filteredIds = new Set(this.filtered.map(b => b.id));

        for (const b of this.filtered) {
          const parent = _getEpicParent(b);
          if (parent && b.issue_type !== 'epic') {
            if (!epicMap[parent]) epicMap[parent] = { epic: null, children: [] };
            epicMap[parent].children.push(b);
          } else if (b.issue_type === 'epic') {
            if (!epicMap[b.id]) epicMap[b.id] = { epic: b, children: [] };
            else epicMap[b.id].epic = b;
          } else {
            orphans.push(b);
          }
        }

        const hasFilters = !!(this.query || this.hasFilters);
        // Remove empty filtered branches
        for (const [epicId, g] of Object.entries(epicMap)) {
          if (hasFilters && !g.children.length && (!g.epic || !filteredIds.has(epicId))) {
            delete epicMap[epicId];
          }
        }

        // All children per epic (for progress bars from full dataset)
        const allChildrenByEpic = {};
        for (const b of this.allBeads) {
          const parent = _getEpicParent(b);
          if (parent && b.issue_type !== 'epic') {
            (allChildrenByEpic[parent] = allChildrenByEpic[parent] || []).push(b);
          }
        }

        const groups = Object.entries(epicMap).map(([epicId, g]) => {
          const epic = g.epic || this.allBeads.find(b => b.id === epicId);
          const allChildren = allChildrenByEpic[epicId] || g.children;
          const closed = allChildren.filter(c => c.status === 'closed').length;
          const total = allChildren.length;
          const pct = total ? Math.round((closed / total) * 100) : 0;
          const barColor = pct === 100 ? 'bg-green-500' : pct > 50 ? 'bg-indigo-500' : 'bg-amber-500';
          const childCount = g.children.length;
          const countLabel = (hasFilters && childCount !== total) ? `${childCount}/${total}` : `${total}`;
          return { epicId, epic, children: g.children, allChildren, closed, total, pct, barColor, countLabel };
        });

        return { groups, orphans };
      },

      // ── Deps view HTML (computed imperatively) ─────────────

      get depsHtml() {
        if (this.loading || !this.filtered.length) return '';
        const graph = _buildDepGraph(this.filtered, this.allBeads);
        const { dependsOn, visibleNodes } = graph;
        if (this.depsMode === 'flat') {
          return _renderDepsFlatHtml(this.filtered, this.terms, graph);
        }
        return _renderDAGHtml(this.filtered, this.terms, this.allBeads);
      },

      get depsStats() {
        if (this.loading) return { blocked: 0, ready: 0, active: 0, total: 0 };
        const graph = _buildDepGraph(this.filtered, this.allBeads);
        const { beadMap, visibleNodes, dependsOn, isolated } = graph;
        const ns = id => _dagNodeStatus(beadMap[id], dependsOn, beadMap);
        return {
          blocked: [...visibleNodes].filter(id => ns(id) === 'blocked').length,
          ready:   [...visibleNodes].filter(id => ns(id) === 'ready').length,
          active:  [...visibleNodes].filter(id => ns(id) === 'active').length,
          total:   visibleNodes.size,
          isolated: graph.isolated.length,
        };
      },

      // ── Actions ───────────────────────────────────────────

      switchView(v) {
        if (this.view === v) return;
        this.view = v;
        this.selected = {};
        localStorage.setItem('beads-view', v);
      },

      sortByCol(col) {
        if (this.sortCol === col) {
          this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          this.sortCol = col;
          this.sortDir = 'asc';
        }
      },

      togglePriority(p) {
        const idx = this.fPriority.indexOf(p);
        if (idx >= 0) this.fPriority.splice(idx, 1);
        else this.fPriority.push(p);
        // Force reactivity since we mutate array
        this.fPriority = [...this.fPriority];
      },

      togglePhase(ph) {
        const idx = this.fPhase.indexOf(ph);
        if (idx >= 0) this.fPhase.splice(idx, 1);
        else this.fPhase.push(ph);
        this.fPhase = [...this.fPhase];
      },

      toggleType(t) {
        const idx = this.fType.indexOf(t);
        if (idx >= 0) this.fType.splice(idx, 1);
        else this.fType.push(t);
        this.fType = [...this.fType];
      },

      toggleLabel(l) {
        const idx = this.fLabels.indexOf(l);
        if (idx >= 0) this.fLabels.splice(idx, 1);
        else this.fLabels.push(l);
        this.fLabels = [...this.fLabels];
      },

      setBlocked(val) {
        this.fBlocked = this.fBlocked === val ? '' : val;
      },

      setCreator(val) {
        this.fCreator = this.fCreator === val ? '' : val;
      },

      clearFilters() {
        this.fPriority = []; this.fPhase = []; this.fType = [];
        this.fLabels = []; this.fLabelMode = 'or';
        this.fEpic = ''; this.fBlocked = ''; this.fCreator = '';
      },

      // List view: toggle row selection
      toggleSelect(id) {
        if (this.selected[id]) {
          const s = { ...this.selected };
          delete s[id];
          this.selected = s;
        } else {
          this.selected = { ...this.selected, [id]: true };
        }
      },

      toggleSelectAll() {
        if (this.selectedCount > 0) {
          this.selected = {};
        } else {
          const s = {};
          for (const b of this.sortedList) s[b.id] = true;
          this.selected = s;
        }
      },

      clearSelection() { this.selected = {}; },

      goToBead(id) { navigateTo('/bead/' + id); },

      // Tree expand/collapse
      _initTreeOpen() {
        const o = {};
        for (const b of this.allBeads) {
          if (b.issue_type === 'epic') o[b.id] = true;
        }
        o['__orphans__'] = true;
        this.treeOpen = o;
        this.treeExpandedAll = true;
      },

      treeToggleAll() {
        this.treeExpandedAll = !this.treeExpandedAll;
        const o = {};
        for (const k of Object.keys(this.treeOpen)) o[k] = this.treeExpandedAll;
        this.treeOpen = o;
      },

      toggleTreeGroup(id) {
        this.treeOpen = { ...this.treeOpen, [id]: !this.treeOpen[id] };
      },

      // Template helpers used inside x-for
      getPhase: _getPhase,
      getEpicParent: _getEpicParent,
      typeIcon: _typeIcon,
      creatorIcon: _creatorIcon,
      highlightText: _highlightText,
      priorityBadgeHtml: _priorityBadgeHtml,
      fmtDate: _fmtDate,

      visibleLabels(b) {
        return (b.labels || []).filter(l => !l.startsWith('readiness:') && !l.startsWith('dispatch:'));
      },

      phaseBadgeClass(phase) {
        const m = { idea: 'bg-gray-600 text-gray-200', draft: 'bg-blue-900 text-blue-300', specified: 'bg-indigo-900 text-indigo-300', approved: 'bg-green-900 text-green-300' };
        return m[phase] || 'bg-gray-700 text-gray-300';
      },

      async bulkApprove() {
        const ids = Object.keys(this.selected);
        if (!ids.length) return;
        const results = await Promise.all(
          ids.map(id => fetch(`/api/bead/${id}/approve`, { method: 'POST' }).then(r => r.json()))
        );
        const failed = results.filter(r => !r.ok);
        if (failed.length) alert(`${failed.length} of ${ids.length} failed to approve`);
        const fresh = await fetch('/api/beads/list').then(r => r.json());
        this.allBeads = Array.isArray(fresh) ? fresh : [];
        this.selected = {};
      },

      // Approve a bead (board / list view inline button)
      async approveBead(id, btnEl) {
        if (btnEl) { btnEl.disabled = true; btnEl.textContent = '...'; }
        const res = await fetch(`/api/bead/${id}/approve`, { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          // Refresh data
          const fresh = await fetch('/api/beads/list').then(r => r.json());
          this.allBeads = Array.isArray(fresh) ? fresh : [];
        } else {
          if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Approve'; }
          alert(`Failed to approve: ${data.error}`);
        }
      },
    }));
  });
})();
