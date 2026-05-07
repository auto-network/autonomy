// Autonomy Dashboard — client-side routing and rendering
// Every view is a function that fetches JSON from the API and renders it.

const content = document.getElementById('content');
const pageTitle = document.getElementById('page-title');
const statsSummary = document.getElementById('stats-summary');
const harnessUsage = document.getElementById('harness-usage');
const globalSearch = document.getElementById('global-search');
const HARNESS_USAGE_SETTINGS_SET_ID = 'dashboard.harness.usage';
const HARNESS_USAGE_STALE_MS = 15 * 60 * 1000;

// ── Screenshot Capture (Design Studio) ───────────────────────
// Persistent MediaStream for tab capture; survives page navigations within SPA.
let _displayStream = null;
let _captureVideo = null;
let _displayCapturePending = false;
let _harnessUsageMode = 'fallback';
let _harnessUsageSchema = null;
let _harnessUsageUnsub = null;
let _harnessUsagePage = 0;

// ── Markdown Rendering ───────────────────────────────────────

// DOMPurify config — mirrors SECURE_CONFIG in markdown.js.
// Defined here so renderMd() doesn't depend on markdown.js load order.
const MARKDOWN_SECURE_CONFIG = {
  ALLOWED_TAGS: ['h1','h2','h3','h4','h5','h6','p','br','hr','ul','ol','li',
                 'blockquote','pre','code','em','strong','del','a','img',
                 'table','thead','tbody','tr','th','td','sup','sub','details','summary'],
  ALLOWED_ATTR: ['href','src','alt','title','class','id','colspan','rowspan','align'],
  ALLOW_DATA_ATTR: false,
  ADD_ATTR: ['target'],
  FORBID_TAGS: ['script','style','iframe','object','embed','form','input',
                'textarea','select','meta','link'],
  FORBID_ATTR: ['onerror','onload','onclick','onmouseover','onfocus','onblur','style'],
};

function renderMd(md) {
  const html = DOMPurify.sanitize(marked.parse(md || ''), MARKDOWN_SECURE_CONFIG);
  const el = document.createElement('div');
  el.className = 'markdown-body';
  el.innerHTML = html;
  el.querySelectorAll('pre code').forEach(block => hljs.highlightElement(block));
  return el;
}

// ── API Helpers ──────────────────────────────────────────────

// Active org for any in-flight fetch. Two slots:
//
//   _activeShellOrg   — the deployment's default org. Stamped once at
//                       module load from the server-injected meta tag
//                       ``<meta name="autonomy-shell-org">``. Applies
//                       to every shell route (``/``, ``/beads``,
//                       ``/timeline``, etc.) so non-plugin consumers
//                       like ``Schema.of(...)`` resolve against the
//                       right org slice instead of falling through to
//                       the server's scopeless default.
//   _activePluginOrg  — set in ``renderPluginFragment`` to the plugin's
//                       effective install org and cleared on every
//                       ``route()`` call. Wins over the shell default
//                       so plugin pages talk to their own org.
//
// `_withOrgHeader` consults both: plugin org first (override), shell
// org as fallback. Bead auto-t0auy fixed the prior behavior where the
// shell sent no header at all, leaving Schema.of(...) silently empty.
window.Autonomy = window.Autonomy || {};
window.Autonomy._activePluginOrg = null;
window.Autonomy._activeShellOrg = null;
// Active plugin id during a plugin's page render. Set alongside
// ``_activePluginOrg`` in ``renderPluginFragment``; consumed by
// ``Schema.alpine`` (bead auto-2D) so plugin pages can self-identify
// without the shell threading the id through manually. Cleared on
// every ``route()`` call before the next fragment renders.
window.Autonomy._activePluginId = null;

(function _bootstrapShellOrg() {
  if (typeof document === 'undefined') return;
  const meta = document.querySelector('meta[name="autonomy-shell-org"]');
  const value = meta && meta.getAttribute('content');
  if (typeof value === 'string' && value) {
    window.Autonomy._activeShellOrg = value;
  }
})();

async function api(path) {
  const res = await fetch(path, _withOrgHeader());
  return res.json();
}

function _withOrgHeader(opts) {
  const org = window.Autonomy._activePluginOrg
           || window.Autonomy._activeShellOrg;
  if (!org) return opts || undefined;
  const init = Object.assign({}, opts || {});
  const headers = new Headers(init.headers || {});
  // Caller-supplied X-Graph-Org wins. The shell/plugin default is just
  // that — a default for callers that don't specify. Cross-org reads
  // (e.g. Schema.prototype.all({headers: {'X-Graph-Org': org}}) from
  // agent-actions.js) need their own org to land on the wire intact.
  if (!headers.has('X-Graph-Org')) headers.set('X-Graph-Org', org);
  init.headers = headers;
  return init;
}

window.Autonomy.fetch = function (path, opts) {
  return fetch(path, _withOrgHeader(opts));
};

// ── Badge Helpers ────────────────────────────────────────────

function priorityBadge(p) {
  return `<span class="badge badge-p${p}">P${p}</span>`;
}

function statusBadge(s) {
  const cls = s === 'closed' ? 'closed' : s === 'in_progress' ? 'in_progress' : s === 'blocked' ? 'blocked' : 'open';
  return `<span class="badge badge-${cls}">${s}</span>`;
}

function formatRatePercent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return `${num % 1 === 0 ? num.toFixed(0) : num.toFixed(1)}%`;
}

function formatRateWindowLabel(windowMinutes) {
  const minutes = Number(windowMinutes);
  if (!Number.isFinite(minutes) || minutes <= 0) return '--';
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

function formatResetLong(epochSeconds) {
  const ts = Number(epochSeconds);
  if (!Number.isFinite(ts) || ts <= 0) return '--';
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return '--';
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatUpdatedAt(timestamp) {
  if (!timestamp) return '--';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '--';
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function harnessWindowTone(windowData) {
  const used = Number(windowData && windowData.used_percent);
  if (!Number.isFinite(used)) return '';
  if (used >= 90) return 'is-hot';
  if (used >= 75) return 'is-warn';
  if (used <= 50) return 'is-cool';
  return '';
}

function formatHarnessUsageCountdown(epochSeconds) {
  const ts = Number(epochSeconds);
  if (!Number.isFinite(ts) || ts <= 0) return '--';
  const diffMs = (ts * 1000) - Date.now();
  if (diffMs <= 0) return '0m';
  const totalMinutes = Math.max(1, Math.ceil(diffMs / 60000));
  if (totalMinutes >= 1440) {
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    return hours > 0 ? `${days}d${hours}h` : `${days}d`;
  }
  if (totalMinutes >= 60) {
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return minutes > 0 ? `${hours}h${minutes}m` : `${hours}h`;
  }
  return `${totalMinutes}m`;
}

function renderHarnessStripWindow(windowData, fallbackLabel) {
  const label = windowData && windowData.window_minutes
    ? formatRateWindowLabel(windowData.window_minutes)
    : fallbackLabel;
  const used = Number(windowData && windowData.used_percent);
  const pct = Number.isFinite(used) ? Math.max(0, Math.min(100, used)) : 0;
  const duration = formatHarnessUsageCountdown(windowData && windowData.resets_at);
  const rowClass = ['harness-strip-row'];
  if (!windowData) rowClass.push('is-unavailable');
  const tone = harnessWindowTone(windowData);
  if (tone) rowClass.push(tone);
  return `<div class="${rowClass.join(' ')}">
    <span class="harness-strip-window">${_esc(label)}</span>
    <span class="harness-strip-rail"><span class="harness-strip-fill" style="width:${pct}%"></span></span>
    <span class="harness-strip-reset">${_esc(duration)}</span>
  </div>`;
}

function renderHarnessUsageDots(count, activeIndex) {
  if (count < 2) return '';
  return `<div class="harness-strip-dots" aria-hidden="true">${Array.from({ length: count }, (_, index) => `
    <span class="harness-strip-dot${index === activeIndex ? ' is-active' : ''}" data-index="${index}"></span>
  `).join('')}</div>`;
}

function clampHarnessUsagePage(index, count) {
  if (!count) return 0;
  return Math.max(0, Math.min(count - 1, index));
}

function syncHarnessUsageScroll(behavior = 'auto') {
  if (!harnessUsage) return;
  const scroller = harnessUsage.querySelector('.harness-strip-scroller');
  if (!scroller) return;
  const width = scroller.clientWidth || 0;
  if (!width) return;
  scroller.scrollTo({ left: width * _harnessUsagePage, behavior });
}

function updateHarnessUsageState(items) {
  if (!harnessUsage) return;
  harnessUsage.querySelectorAll('.harness-strip-page').forEach((page) => {
    const pageIndex = Number(page.dataset.index || 0);
    page.classList.toggle('is-active', pageIndex === _harnessUsagePage);
  });
  harnessUsage.querySelectorAll('.harness-strip-hit').forEach((button) => {
    button.setAttribute('aria-expanded', 'false');
  });
  harnessUsage.querySelectorAll('.harness-strip-dot').forEach((dot) => {
    const dotIndex = Number(dot.dataset.index || 0);
    dot.classList.toggle('is-active', dotIndex === _harnessUsagePage);
  });
}

function attachHarnessUsageInteractions(items) {
  if (!harnessUsage) return;
  const scroller = harnessUsage.querySelector('.harness-strip-scroller');
  if (!scroller) return;

  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let dragDistance = 0;
  let scrollTimer = 0;

  const onScrollSettled = () => {
    const width = scroller.clientWidth || 1;
    const nextPage = clampHarnessUsagePage(
      Math.round(scroller.scrollLeft / width),
      items.length,
    );
    if (nextPage !== _harnessUsagePage) {
      _harnessUsagePage = nextPage;
      updateHarnessUsageState(items);
    }
  };

  scroller.addEventListener('pointerdown', (event) => {
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    dragDistance = 0;
  });

  scroller.addEventListener('pointermove', (event) => {
    if (pointerId !== event.pointerId) return;
    dragDistance = Math.max(
      dragDistance,
      Math.abs(event.clientX - startX),
      Math.abs(event.clientY - startY),
    );
  });

  const clearPointer = (event) => {
    if (pointerId !== event.pointerId) return;
    pointerId = null;
    window.setTimeout(() => { dragDistance = 0; }, 0);
  };

  scroller.addEventListener('pointerup', clearPointer);
  scroller.addEventListener('pointercancel', clearPointer);

  scroller.addEventListener('scroll', () => {
    window.clearTimeout(scrollTimer);
    scrollTimer = window.setTimeout(onScrollSettled, 60);
  });

  harnessUsage.querySelectorAll('.harness-strip-hit').forEach((button) => {
    button.addEventListener('click', (event) => {
      if (dragDistance > 8) {
        event.preventDefault();
        return;
      }
      const pageIndex = clampHarnessUsagePage(
        Number(button.dataset.index || 0),
        items.length,
      );
      if (pageIndex !== _harnessUsagePage) {
        _harnessUsagePage = pageIndex;
        updateHarnessUsageState(items);
        syncHarnessUsageScroll('smooth');
      }
    });
  });
}

function normalizeLegacyHarnessUsageItem(item) {
  const state = item && item.state ? item.state : {};
  return {
    harness: item && item.harness ? item.harness : 'unknown',
    metaLabel: `${item && item.session_count ? item.session_count : 0} live`,
    status: item && item.available ? 'ok' : 'unavailable',
    source: state.source || null,
    updatedAt: state.updated_at || null,
    accountId: null,
    identityLabel: null,
    planType: state.plan_type || null,
    tier: null,
    limitId: state.limit_id || null,
    limitName: state.limit_name || null,
    rateLimitReachedType: state.rate_limit_reached_type || null,
    note: item && item.reason ? item.reason : null,
    windows: state.windows || {},
  };
}

function normalizeSettingsHarnessUsageItem(item) {
  return {
    harness: item && item.harness ? item.harness : 'unknown',
    metaLabel: item && item.identity_label ? item.identity_label : '',
    status: item && item.status ? item.status : 'unknown',
    source: item && item.source ? item.source : null,
    updatedAt: item && item.updated_at ? item.updated_at : null,
    accountId: item && item.account_id ? item.account_id : null,
    identityLabel: item && item.identity_label ? item.identity_label : null,
    planType: item && item.plan_type ? item.plan_type : null,
    tier: item && item.tier ? item.tier : null,
    limitId: item && item.limit_id ? item.limit_id : null,
    limitName: item && item.limit_name ? item.limit_name : null,
    rateLimitReachedType: item && item.rate_limit_reached_type ? item.rate_limit_reached_type : null,
    note: item && item.note ? item.note : null,
    windows: item && item.windows ? item.windows : {},
  };
}

function normalizeHarnessUsageItems(data) {
  const rawItems = Array.isArray(data && data.harnesses) ? data.harnesses : [];
  return rawItems.map(item => {
    if (item && Object.prototype.hasOwnProperty.call(item, 'status')) {
      return normalizeSettingsHarnessUsageItem(item);
    }
    return normalizeLegacyHarnessUsageItem(item);
  });
}

function renderHarnessUsage(data) {
  if (!harnessUsage) return;
  const items = normalizeHarnessUsageItems(data);
  if (!items.length) {
    harnessUsage.classList.remove('has-tiles');
    _harnessUsagePage = 0;
    harnessUsage.innerHTML = '';
    return;
  }
  _harnessUsagePage = clampHarnessUsagePage(_harnessUsagePage, items.length);
  harnessUsage.classList.add('has-tiles');
  harnessUsage.innerHTML = `
    <div class="harness-strip">
      <div class="harness-strip-scroller">
        ${items.map((item, index) => {
          const windows = item.windows || {};
          return `<div class="harness-strip-page${index === _harnessUsagePage ? ' is-active' : ''}" data-index="${index}">
            <button
              type="button"
              class="harness-strip-hit"
              data-index="${index}"
              aria-expanded="false"
            >
              <div class="harness-strip-head">
                <span class="harness-strip-label">${_esc(item.harness || 'unknown')}</span>
                ${renderHarnessUsageDots(items.length, _harnessUsagePage)}
              </div>
              <div class="harness-strip-bars">
                ${renderHarnessStripWindow(windows.short, '5h')}
                ${renderHarnessStripWindow(windows.long, '7d')}
              </div>
            </button>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  attachHarnessUsageInteractions(items);
  updateHarnessUsageState(items);
  syncHarnessUsageScroll();
}

function freshHarnessUsageSettings(members) {
  const now = Date.now();
  const items = [];
  (Array.isArray(members) ? members : []).forEach(member => {
    const payload = member && member.payload;
    if (!payload || typeof payload !== 'object') return;
    if (payload.status && payload.status !== 'ok') return;
    const updatedAt = member.updated_at || payload.updated_at;
    const ts = updatedAt ? Date.parse(updatedAt) : NaN;
    if (!Number.isFinite(ts)) return;
    if ((now - ts) > HARNESS_USAGE_STALE_MS) return;
    items.push({ ...payload, updated_at: updatedAt });
  });
  items.sort((a, b) => {
    const order = { claude: 0, codex: 1 };
    const byHarness = (order[a.harness] ?? 99) - (order[b.harness] ?? 99);
    if (byHarness) return byHarness;
    const statusOrder = { ok: 0, unknown: 1, unavailable: 2 };
    const byStatus = (statusOrder[a.status] ?? 99) - (statusOrder[b.status] ?? 99);
    if (byStatus) return byStatus;
    const tsA = Date.parse(a.updated_at || a.updatedAt || '');
    const tsB = Date.parse(b.updated_at || b.updatedAt || '');
    if (Number.isFinite(tsA) && Number.isFinite(tsB) && tsA !== tsB) return tsB - tsA;
    return String(a.identity_label || '').localeCompare(String(b.identity_label || ''));
  });
  return items;
}

async function refreshHarnessUsageFromSettings() {
  if (!_harnessUsageSchema) return 0;
  const members = await _harnessUsageSchema.all();
  const items = freshHarnessUsageSettings(members);
  if (items.length) {
    _harnessUsageMode = 'settings';
    renderHarnessUsage({ harnesses: items });
  } else if (_harnessUsageMode === 'settings') {
    renderHarnessUsage({ harnesses: [] });
  }
  return items.length;
}

async function initHarnessUsageSettings() {
  if (!(window.Schema && typeof window.Schema.of === 'function')) return;
  try {
    _harnessUsageSchema = await window.Schema.of(HARNESS_USAGE_SETTINGS_SET_ID);
    if (_harnessUsageUnsub) _harnessUsageUnsub();
    _harnessUsageUnsub = _harnessUsageSchema.onChange(() => {
      refreshHarnessUsageFromSettings().catch(() => {});
    });
    await refreshHarnessUsageFromSettings();
  } catch (_) {
    _harnessUsageSchema = null;
  }
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

// Replace a host element's contents and rebind Alpine, ensuring the
// outgoing tree's components run their destroy() callbacks.
//
// Why this matters: setting `host.innerHTML = ...` removes the prior
// DOM, but Alpine's MutationObserver can race the replacement and skip
// firing destroy() on the components that were attached to the old
// subtree. Anything those components registered against window/document
// (event listeners, intervals, SSE subscriptions) leaks across the
// navigation. After enough page transitions, a single dispatched event
// fires N stacked listeners — which manifests as N parallel API calls
// from one user action.
//
// destroyTree explicitly walks the subtree first and runs every
// component's destroy(), so subsequent initTree starts from a clean
// slate.
function _replaceFragment(host, html) {
  if (window.Alpine) {
    Array.from(host.children).forEach(child => Alpine.destroyTree(child));
  }
  host.innerHTML = html;
  if (window.Alpine) {
    // Some fragments begin with sibling <style> tags before their x-data
    // root. Initialize every top-level child so Alpine doesn't skip the
    // reactive root when it's not the first element.
    Array.from(host.children).forEach(child => Alpine.initTree(child));
  }
}

// ── Beads Page (Jinja2 fragment + Alpine) ────────────────────

async function renderBeadsFragment() {
  pageTitle.textContent = 'Beads';
  let html;
  if (_fragmentCache.has('/pages/beads')) {
    html = _fragmentCache.get('/pages/beads');
  } else {
    const res = await fetch('/pages/beads');
    html = await res.text();
    _fragmentCache.set('/pages/beads', html);
  }
  _replaceFragment(content, html);
}


// ── Bead Detail Page (Jinja2 fragment + Alpine) ─────────────────

async function renderBeadDetailFragment(id) {
  pageTitle.textContent = `Bead: ${id}`;
  let html;
  if (_fragmentCache.has('/pages/bead')) {
    html = _fragmentCache.get('/pages/bead');
  } else {
    const res = await fetch('/pages/bead');
    html = await res.text();
    _fragmentCache.set('/pages/bead', html);
  }
  _replaceFragment(content, html);
}

// ── Dispatch Page (Jinja2 fragment + Alpine) ──────────────────

const _fragmentCache = new Map();
let _serverVersion = null;

async function _checkVersion() {
  try {
    const { version } = await fetch('/api/version').then(r => r.json());
    if (_serverVersion && _serverVersion !== version) {
      _fragmentCache.clear();
    }
    _serverVersion = version;
  } catch (_) {}
}

async function renderDispatchFragment() {
  pageTitle.textContent = 'Dispatch';
  let html;
  if (_fragmentCache.has('/pages/dispatch')) {
    html = _fragmentCache.get('/pages/dispatch');
  } else {
    const res = await fetch('/pages/dispatch');
    html = await res.text();
    _fragmentCache.set('/pages/dispatch', html);
  }
  _replaceFragment(content, html);
}


// ── Timeline Page (Jinja2 fragment + Alpine) ──────────────────

async function renderTimelineFragment() {
  pageTitle.textContent = 'Activity';
  let html;
  if (_fragmentCache.has('/pages/timeline')) {
    html = _fragmentCache.get('/pages/timeline');
  } else {
    const res = await fetch('/pages/timeline');
    html = await res.text();
    _fragmentCache.set('/pages/timeline', html);
  }
  _replaceFragment(content, html);
}

// ── Trace Page (Jinja2 fragment + Alpine) ──────────────────────

async function renderTraceFragment() {
  pageTitle.textContent = 'Trace';
  let html;
  if (_fragmentCache.has('/pages/trace')) {
    html = _fragmentCache.get('/pages/trace');
  } else {
    const res = await fetch('/pages/trace');
    html = await res.text();
    _fragmentCache.set('/pages/trace', html);
  }
  _replaceFragment(content, html);
}




async function renderSessionsFragment() {
  pageTitle.textContent = 'Sessions';
  let html;
  if (_fragmentCache.has('/pages/sessions')) {
    html = _fragmentCache.get('/pages/sessions');
  } else {
    const res = await fetch('/pages/sessions');
    html = await res.text();
    _fragmentCache.set('/pages/sessions', html);
  }
  _replaceFragment(content, html);
}

async function renderWorktreesFragment() {
  pageTitle.textContent = 'Worktrees';
  let html;
  if (_fragmentCache.has('/pages/worktrees')) {
    html = _fragmentCache.get('/pages/worktrees');
  } else {
    const res = await fetch('/pages/worktrees');
    html = await res.text();
    _fragmentCache.set('/pages/worktrees', html);
  }
  if (window.location.pathname !== '/worktrees') return;
  _replaceFragment(content, html);
}

async function renderSessionViewFragment() {
  pageTitle.textContent = 'Session';
  let html;
  if (_fragmentCache.has('/pages/session-view')) {
    html = _fragmentCache.get('/pages/session-view');
  } else {
    const res = await fetch('/pages/session-view');
    html = await res.text();
    _fragmentCache.set('/pages/session-view', html);
  }
  _replaceFragment(content, html);
}

async function renderSourceFragment() {
  const path = window.location.pathname;
  const id = path.split('/graph/')[1] || path.split('/source/')[1] || '';
  pageTitle.textContent = `Source: ${id.slice(0, 12)}`;
  let html;
  if (_fragmentCache.has('/pages/source')) {
    html = _fragmentCache.get('/pages/source');
  } else {
    const res = await fetch('/pages/source');
    html = await res.text();
    _fragmentCache.set('/pages/source', html);
  }
  _replaceFragment(content, html);
}

// ── Search Results Page (Jinja2 fragment + Alpine) ───────────

async function renderSearchFragment() {
  pageTitle.textContent = 'Search';
  let html;
  if (_fragmentCache.has('/pages/search')) {
    html = _fragmentCache.get('/pages/search');
  } else {
    const res = await fetch('/pages/search');
    html = await res.text();
    _fragmentCache.set('/pages/search', html);
  }
  _replaceFragment(content, html);
}

// ── Streams Landing Page (Jinja2 fragment + Alpine) ──────────

async function renderStreamsFragment() {
  pageTitle.textContent = 'Streams';
  let html;
  if (_fragmentCache.has('/pages/streams')) {
    html = _fragmentCache.get('/pages/streams');
  } else {
    const res = await fetch('/pages/streams');
    html = await res.text();
    _fragmentCache.set('/pages/streams', html);
  }
  _replaceFragment(content, html);
}

// ── Collab Hub Page (Jinja2 fragment + Alpine) ──────────────

async function renderCollabFragment() {
  pageTitle.textContent = 'Collab';
  let html;
  if (_fragmentCache.has('/pages/collab')) {
    html = _fragmentCache.get('/pages/collab');
  } else {
    const res = await fetch('/pages/collab');
    html = await res.text();
    _fragmentCache.set('/pages/collab', html);
  }
  _replaceFragment(content, html);
}

// ── Stream Page (Jinja2 fragment + Alpine) ───────────────────

async function renderStreamFragment() {
  const path = window.location.pathname;
  const tag = decodeURIComponent(path.split('/stream/')[1] || '');
  pageTitle.textContent = `#${tag}`;
  let html;
  if (_fragmentCache.has('/pages/stream')) {
    html = _fragmentCache.get('/pages/stream');
  } else {
    const res = await fetch('/pages/stream');
    html = await res.text();
    _fragmentCache.set('/pages/stream', html);
  }
  _replaceFragment(content, html);
}

// ── Experiment Page (Jinja2 fragment + Alpine) ───────────────

async function renderDesignFragment() {
  pageTitle.textContent = 'Design';
  // Belt-and-suspenders: clean up any SSE subscription before replacing DOM
  // (Alpine destroy() will also fire, but ordering is not guaranteed).
  if (window._designSeriesCleanup) {
    window._designSeriesCleanup();
    window._designSeriesCleanup = null;
  }
  let html;
  if (_fragmentCache.has('/pages/design')) {
    html = _fragmentCache.get('/pages/design');
  } else {
    const res = await fetch('/pages/design');
    html = await res.text();
    _fragmentCache.set('/pages/design', html);
  }
  _replaceFragment(content, html);
}

// ── Chat With Panel ─────────────────────────────────────────

let _chatWithTerm = null;
let _chatWithWs = null;
let _chatWithFitAddon = null;
let _chatWithResizeObs = null;
let _chatWithCollapsed = false;

function destroyChatWith() {
  if (_chatWithResizeObs) { _chatWithResizeObs.disconnect(); _chatWithResizeObs = null; }
  if (_chatWithWs) { try { _chatWithWs.close(); } catch(e) {} _chatWithWs = null; }
  if (_chatWithTerm) { _chatWithTerm.dispose(); _chatWithTerm = null; }
  _chatWithFitAddon = null;
}

// Called by the design Alpine component when expanding the Chat With panel.
function _fitChatWithAddon() {
  if (_chatWithFitAddon) _chatWithFitAddon.fit();
}

function connectChatWithTerminal(sessionName) {
  destroyChatWith();

  const container = document.getElementById('chatwith-container');
  if (!container) return;

  // Use Alpine bridge if the design Alpine component is active; otherwise
  // fall back to direct DOM manipulation for the legacy (non-Alpine) path.
  const page = window._designPage;
  if (page) {
    page.showChatWithPanel();
    page.setKillVisible(true);
    page.setChatWithStatus('connecting...', 'text-xs text-yellow-400 ml-2');
  } else {
    const panel = document.getElementById('chatwith-panel');
    const body = document.getElementById('chatwith-body');
    const killBtn = document.getElementById('chatwith-kill-btn');
    const statusEl = document.getElementById('chatwith-status');
    if (panel) panel.style.display = '';
    if (body && !_chatWithCollapsed) body.style.display = '';
    if (killBtn) killBtn.style.display = '';
    if (statusEl) { statusEl.textContent = 'connecting...'; statusEl.className = 'text-xs text-yellow-400 ml-2'; }
  }

  const term = new Terminal({
    theme: {background:'#111827',foreground:'#e5e7eb',cursor:'#6366f1',selectionBackground:'rgba(99,102,241,0.3)'},
    fontSize: 13,
    fontFamily: '"JetBrains Mono",ui-monospace,monospace',
    cursorBlink: true,
    scrollback: 5000,
  });
  const fitAddon = new FitAddon.FitAddon();
  _chatWithFitAddon = fitAddon;
  term.loadAddon(fitAddon);
  term.open(container);
  fitAddon.fit();
  _chatWithTerm = term;

  _chatWithResizeObs = new ResizeObserver(() => { if (_chatWithFitAddon) _chatWithFitAddon.fit(); });
  _chatWithResizeObs.observe(container);

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${location.host}/ws/terminal?attach=${encodeURIComponent(sessionName)}`;
  const ws = new WebSocket(wsUrl);
  _chatWithWs = ws;

  ws.onopen = () => {
    if (page) {
      page.setChatWithStatus('connected', 'text-xs text-green-400 ml-2');
      page.setReconnectVisible(false);
    } else {
      const statusEl = document.getElementById('chatwith-status');
      const reconnectBtn = document.getElementById('chatwith-reconnect-btn');
      if (statusEl) { statusEl.textContent = 'connected'; statusEl.className = 'text-xs text-green-400 ml-2'; }
      if (reconnectBtn) reconnectBtn.style.display = 'none';
    }
    const dims = fitAddon.proposeDimensions();
    if (dims) ws.send(`\x1b[8;${dims.rows};${dims.cols}t`);
    term.focus();
  };
  ws.onmessage = (e) => term.write(e.data);
  ws.onclose = () => {
    if (page) {
      page.setChatWithStatus('disconnected', 'text-xs text-red-400 ml-2');
      page.setReconnectVisible(true);
    } else {
      const statusEl = document.getElementById('chatwith-status');
      const reconnectBtn = document.getElementById('chatwith-reconnect-btn');
      if (statusEl) { statusEl.textContent = 'disconnected'; statusEl.className = 'text-xs text-red-400 ml-2'; }
      if (reconnectBtn) reconnectBtn.style.display = '';
    }
    term.write('\r\n\x1b[90m--- disconnected ---\x1b[0m\r\n');
  };
  ws.onerror = () => {
    if (page) {
      page.setChatWithStatus('error', 'text-xs text-red-400 ml-2');
      page.setReconnectVisible(true);
    } else {
      const statusEl = document.getElementById('chatwith-status');
      const reconnectBtn = document.getElementById('chatwith-reconnect-btn');
      if (statusEl) { statusEl.textContent = 'error'; statusEl.className = 'text-xs text-red-400 ml-2'; }
      if (reconnectBtn) reconnectBtn.style.display = '';
    }
  };
  term.onData((data) => { if (ws.readyState === WebSocket.OPEN) ws.send(data); });
}

function toggleChatWithPanel() {
  const body = document.getElementById('chatwith-body');
  const toggleBtn = document.getElementById('chatwith-toggle-btn');
  if (!body) return;
  _chatWithCollapsed = !_chatWithCollapsed;
  body.style.display = _chatWithCollapsed ? 'none' : '';
  if (toggleBtn) toggleBtn.textContent = _chatWithCollapsed ? '▸' : '▾';
  if (!_chatWithCollapsed && _chatWithFitAddon) setTimeout(() => _chatWithFitAddon.fit(), 50);
}

async function killChatWithSession(designId) {
  const sessionName = `chatwith-${designId}`;
  destroyChatWith();
  const panel = document.getElementById('chatwith-panel');
  if (panel) panel.style.display = 'none';
  const btn = document.getElementById('chatwith-btn');
  if (btn) { btn.textContent = 'Chat With'; btn.disabled = false; }
  await fetch(`/api/terminal/${sessionName}/kill`);
}


// ── Terminal ─────────────────────────────────────────────────

let _activeTermInstance = null;  // result of window.mountTerminal(), or null
let activeTerminalId = null;
let _terminalFragmentInit = false;

function destroyTerminal() {
  if (_activeTermInstance) {
    try { _activeTermInstance.dispose(); } catch (e) {}
    _activeTermInstance = null;
  }
}

async function renderTerminalFragment() {
  if (_terminalFragmentInit) {
    // Alpine already initialized — sync state and refresh pills
    if (window._terminalPage) {
      window._terminalPage.activeId = activeTerminalId;
      await window._terminalPage.refresh();
    }
    return;
  }
  let html;
  if (_fragmentCache.has('/pages/terminal')) {
    html = _fragmentCache.get('/pages/terminal');
  } else {
    const res = await fetch('/pages/terminal');
    html = await res.text();
    _fragmentCache.set('/pages/terminal', html);
  }
  const termPage = document.getElementById('terminal-page');
  _replaceFragment(termPage, html);
  _terminalFragmentInit = true;
}

// Map legacy cmd strings to /api/session/create request bodies.
// ws_terminal is attach-only — all creation goes through REST.
function _createBodyForCmd(cmd) {
  if (!cmd) return null;
  if (cmd === 'autonomy-agent-claude') return {};
  if (cmd.startsWith('claude')) return {type: 'host'};
  return null;
}

async function renderTerminal(cmd, attach) {
  // Auto-reconnect to previously active session when navigating back
  if (!cmd && !attach && activeTerminalId) {
    attach = activeTerminalId;
  }

  // When given a cmd, create the session via REST first, then attach.
  if (cmd && !attach) {
    const body = _createBodyForCmd(cmd);
    if (!body) {
      console.warn('[renderTerminal] unsupported cmd:', cmd);
      return;
    }
    try {
      const res = await fetch('/api/session/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!res.ok && res.status !== 202) {
        const err = await res.json().catch(() => ({}));
        console.warn('[renderTerminal] create failed:', err);
        return;
      }
      const data = await res.json();
      if (!data.tmux_name) {
        console.warn('[renderTerminal] create returned no tmux_name');
        return;
      }
      attach = data.tmux_name;
      cmd = null;
    } catch (err) {
      console.warn('[renderTerminal] create error:', err);
      return;
    }
  }

  pageTitle.textContent = attach ? `Attached: ${attach}` : 'Terminal';

  // Ensure Alpine chrome is rendered (idempotent after first call)
  await renderTerminalFragment();

  // If we already have an active connection to the requested session, just re-fit
  if (attach && _activeTermInstance && _activeTermInstance.ws
      && _activeTermInstance.ws.readyState === WebSocket.OPEN
      && activeTerminalId === attach) {
    _activeTermInstance.fit();
    try { _activeTermInstance.term.focus(); } catch (e) {}
    return;
  }

  // Update active terminal ID
  if (attach) {
    activeTerminalId = attach;
    if (window._terminalPage) window._terminalPage.setActiveId(attach);
  }

  if (!attach) return;

  // Need to create or switch session — destroy previous
  destroyTerminal();

  const termContainer = document.getElementById('terminal-container');

  _activeTermInstance = window.mountTerminal(termContainer, attach, {
    onStatus: function (state, _msg) {
      if (!window._terminalPage) return;
      if (state === 'connecting') {
        window._terminalPage.setStatus('connecting...', 'text-xs text-yellow-400');
      } else if (state === 'connected') {
        window._terminalPage.setStatus('connected', 'text-xs text-green-400');
      } else if (state === 'disconnected') {
        window._terminalPage.setStatus('disconnected', 'text-xs text-red-400');
      } else if (state === 'error') {
        window._terminalPage.setStatus('error', 'text-xs text-red-400');
      }
    },
    onOpen: function () {
      // Refresh pill bar so the new session appears
      if (window._terminalPage) {
        try { window._terminalPage.refresh(); } catch (e) {}
      }
    },
  });
}

// ── Live Session Panel ───────────────────────────────────────
// Panel shell (show/hide/collapse) is imperative; body is Alpine-managed
// via the unified session viewer (session-viewer.js, overlay mode).

function _showPanel(runDir, isLive) {
  const panel = document.getElementById('live-panel');
  const beadLabel = document.getElementById('live-panel-bead');
  const statusEl = document.getElementById('live-panel-status');
  const pulseEl = document.getElementById('live-pulse');
  const badgeEl = document.getElementById('live-panel-badge');

  // Extract bead ID from run dir name (format: <bead>-YYYYMMDD-HHMMSS)
  const parts = runDir.split('-');
  const beadId = parts.length >= 3 ? parts.slice(0, -2).join('-') : runDir;
  beadLabel.textContent = beadId;

  if (isLive) {
    badgeEl.textContent = 'Live';
    badgeEl.className = 'badge badge-open';
    pulseEl.style.animation = '';
    pulseEl.style.background = '#22c55e';
    statusEl.textContent = 'connecting...';
    statusEl.className = 'text-xs text-gray-500 ml-auto';
  } else {
    badgeEl.textContent = 'Complete';
    badgeEl.className = 'badge badge-closed';
    pulseEl.style.background = '#6b7280';
    pulseEl.style.animation = 'none';
    statusEl.textContent = 'loading...';
    statusEl.className = 'text-xs text-gray-500 ml-auto';
  }

  // Show panel
  panel.style.display = 'flex';
  panel.classList.remove('collapsed');
  document.getElementById('content').style.paddingBottom = '20rem';
  const viewer = panel.querySelector('.session-viewer[data-mode="overlay"]');
  if (viewer) viewer.style.display = '';

  // Delegate to Alpine component
  if (window._livePanelLoad) {
    window._livePanelLoad(runDir, isLive);
  }
}

function showLivePanel(runDir) {
  _showPanel(runDir, true);
}

async function showCompletedPanel(runDir) {
  _showPanel(runDir, false);
}

function hideLivePanel() {
  const panel = document.getElementById('live-panel');
  panel.style.display = 'none';
  panel.classList.add('collapsed');
  document.getElementById('content').style.paddingBottom = '';
  // Also hide the .session-viewer[data-mode="overlay"] mount so external
  // selectors (getComputedStyle(.session-viewer)) treat it as gone.
  const viewer = panel.querySelector('.session-viewer[data-mode="overlay"]');
  if (viewer) viewer.style.display = 'none';
  if (window._livePanelReset) window._livePanelReset();
}

function toggleLivePanel() {
  const panel = document.getElementById('live-panel');
  panel.classList.toggle('collapsed');
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

// ── Experiment Gallery ────────────────────────────────────────

/**
 * Request a tab display stream once per session.
 * getDisplayMedia requires a user gesture OR page load in some browsers.
 * Falls back gracefully if unavailable or denied.
 */
async function initDisplayCapture(expId) {
  if (_displayStream || _displayCapturePending) return; // already have a live stream or prompt pending
  if (!navigator.mediaDevices?.getDisplayMedia) {
    console.warn('[screenshot] getDisplayMedia not available (non-HTTPS?)');
    _updateScreenshotStatus(expId, 'Auto-capture unavailable — use Capture button');
    return;
  }
  _displayCapturePending = true;
  try {
    _displayStream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: 'browser' },
      audio: false,
    });
    _captureVideo = document.createElement('video');
    _captureVideo.srcObject = _displayStream;
    _captureVideo.muted = true;
    _captureVideo.play().catch(() => {});
    _displayStream.getVideoTracks()[0]?.addEventListener('ended', () => {
      _displayStream = null;
      _captureVideo = null;
      _updateScreenshotStatus(expId, 'Capture stream ended — click Capture to restart');
      _showCaptureIndicator(false);
      _updateNavIndicators();
    });
    _showCaptureIndicator(true);
    _updateNavIndicators();
    _updateScreenshotStatus(expId, 'Auto-capture active');
    // Stream is now ready — capture after a short delay for rendering
    setTimeout(() => captureTabScreenshot(expId), 1500);
  } catch (e) {
    console.warn('[screenshot] getDisplayMedia denied or failed:', e.message);
    _displayStream = null;
    _captureVideo = null;
    _showCaptureIndicator(false);
    _updateNavIndicators();
    _updateScreenshotStatus(expId, 'Auto-capture denied — use Capture button');
  } finally {
    _displayCapturePending = false;
  }
}

/** Stop display capture: kill the track and clear all global state. */
function stopDisplayCapture() {
  if (_displayStream) {
    _displayStream.getVideoTracks().forEach(t => t.stop());
  }
  _displayStream = null;
  _captureVideo = null;
  _showCaptureIndicator(false);
  _updateNavIndicators();
}

/** Show/hide the REC indicator in the nav toolbar. */
function _showCaptureIndicator(show) {
  const el = document.getElementById('indicator-capture');
  if (!el) return;
  el.style.display = show ? 'flex' : 'none';
}

/** Show nav-indicators toolbar if any child is visible, hide otherwise. */
function _updateNavIndicators() {
  const bar = document.getElementById('nav-indicators');
  if (!bar) return;
  const hasVisible = Array.from(bar.children).some(c => c.style.display !== 'none');
  bar.classList.toggle('active', hasVisible);
}

function _updateScreenshotStatus(expId, msg) {
  Screenshot._updateScreenshotStatus(expId, msg);
}

/**
 * Build screenshot API URL, optionally including tmux_session for two-send injection.
 */
function _screenshotUrl(expId, sessionName) {
  return Screenshot._screenshotUrl(expId, sessionName);
}

/**
 * Handle screenshot response — update status and trigger panel indicator.
 */
function _handleScreenshotResponse(expId, data) {
  Screenshot._handleScreenshotResponse(expId, data);
}

/** Grab a frame from the active display stream and POST to server. */
async function captureTabScreenshot(expId, sessionName) {
  if (!_captureVideo || !_displayStream) return;
  const track = _displayStream.getVideoTracks()[0];
  if (!track || track.readyState !== 'live') return;
  // Wait for video to have dimensions (first frame may not be ready yet)
  for (let i = 0; i < 10 && !_captureVideo.videoWidth; i++) {
    await new Promise(r => setTimeout(r, 300));
  }
  const canvas = document.createElement('canvas');
  canvas.width = _captureVideo.videoWidth;
  canvas.height = _captureVideo.videoHeight;
  if (!canvas.width || !canvas.height) return;
  canvas.getContext('2d').drawImage(_captureVideo, 0, 0);
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
  if (!blob) return;
  try {
    const res = await fetch(_screenshotUrl(expId, sessionName), {
      method: 'POST',
      headers: { 'Content-Type': 'image/png' },
      body: blob,
    });
    if (res.ok) {
      const data = await res.json();
      _handleScreenshotResponse(expId, data);
    }
  } catch (e) {
    console.warn('[screenshot] Upload failed:', e.message);
  }
}

/** Load html2canvas script into a document (parent or iframe). Returns the html2canvas function. */
async function _ensureHtml2Canvas(doc, win) {
  if (win.html2canvas) return win.html2canvas;
  await new Promise((resolve, reject) => {
    const s = doc.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/html2canvas@1/dist/html2canvas.min.js';
    s.onload = resolve;
    s.onerror = reject;
    (doc.head || doc.documentElement).appendChild(s);
  });
  return win.html2canvas;
}

/**
 * Capture experiment variant by running html2canvas inside the same-origin iframe.
 * Works on mobile (iOS Safari) where getDisplayMedia is unavailable.
 * Returns true on success, false on failure.
 */
async function _captureViaIframeHtml2Canvas(expId, sessionName) {
  const iframe = document.querySelector('iframe.design-variant-iframe[data-variant]');
  if (!iframe) return false;
  try {
    const iframeDoc = iframe.contentDocument || iframe.contentWindow?.document;
    const iframeWin = iframe.contentWindow;
    if (!iframeDoc || !iframeWin) return false;
    const h2c = await _ensureHtml2Canvas(iframeDoc, iframeWin);
    if (!h2c) return false;
    const canvas = await h2c(iframeDoc.body, {
      useCORS: true, allowTaint: true, logging: false,
    });
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob) return false;
    const res = await fetch(_screenshotUrl(expId, sessionName), {
      method: 'POST',
      headers: { 'Content-Type': 'image/png' },
      body: blob,
    });
    if (res.ok) {
      const data = await res.json();
      _handleScreenshotResponse(expId, data);
    }
    return true;
  } catch (e) {
    console.warn('[screenshot] iframe html2canvas failed:', e.message);
    return false;
  }
}

/** Fallback: capture visible page using html2canvas (same-origin, no getDisplayMedia needed). */
async function _captureWithHtml2Canvas(expId, sessionName) {
  // Try iframe-based capture first (works on mobile where parent can't see into iframes)
  if (await _captureViaIframeHtml2Canvas(expId, sessionName)) return;
  // Fall back to parent-page capture
  try {
    const h2c = await _ensureHtml2Canvas(document, window);
    const canvas = await h2c(document.getElementById('content'), {
      useCORS: true, allowTaint: true, logging: false,
    });
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob) return;
    const res = await fetch(_screenshotUrl(expId, sessionName), {
      method: 'POST',
      headers: { 'Content-Type': 'image/png' },
      body: blob,
    });
    if (res.ok) {
      const data = await res.json();
      _handleScreenshotResponse(expId, data);
    }
  } catch (e) {
    console.warn('[screenshot] html2canvas fallback failed:', e.message);
    _updateScreenshotStatus(expId, 'Capture failed — check console');
  }
}

/**
 * Manual capture button handler.
 * Uses active stream if available; otherwise tries to acquire one (user gesture
 * helps on some browsers). Falls back to html2canvas if stream cannot be obtained.
 */
async function manualCaptureScreenshot(expId, sessionName) {
  _updateScreenshotStatus(expId, 'Capturing...');
  if (_displayStream) {
    await captureTabScreenshot(expId, sessionName);
    return;
  }
  // Try to acquire stream via user gesture
  try {
    await initDisplayCapture(expId);
    if (_displayStream) {
      await new Promise(r => setTimeout(r, 300)); // let video initialize
      await captureTabScreenshot(expId, sessionName);
      return;
    }
  } catch (e) { /* fall through */ }
  // Fall back to html2canvas
  await _captureWithHtml2Canvas(expId, sessionName);
}

async function renderExperiment(revisionId) {
  // Clean up any previous design SSE subscription
  if (window._designSeriesCleanup) {
    window._designSeriesCleanup();
    window._designSeriesCleanup = null;
  }

  pageTitle.textContent = 'Design';
  _replaceFragment(content, '<div class="text-gray-400">Loading design...</div>');
  destroyChatWith();
  _chatWithCollapsed = false;

  const exp = await api(`/api/design/${revisionId}/full`);
  if (exp.error) {
    _replaceFragment(content, `<div class="text-red-400">Design not found</div>`);
    return;
  }

  const variants = exp.variants || [];
  const isCompleted = exp.status === 'completed';

  // Session context: use design_id when available so all design revisions
  // share one persistent Chat With session ("chatwith-{design_id}")
  const sessionCtx = exp.design_id || revisionId;

  // Revision navigation
  const revisionIds = exp.revisions || [revisionId];
  const revisionIdx = revisionIds.indexOf(revisionId);
  const revisionCount = revisionIds.length;
  const isInSeries = revisionCount > 1;
  const prevId = isInSeries && revisionIdx > 0 ? revisionIds[revisionIdx - 1] : null;
  const nextId = isInSeries && revisionIdx < revisionCount - 1 ? revisionIds[revisionIdx + 1] : null;

  // Track selection state
  const selected = new Map();
  if (isCompleted) {
    variants.forEach(v => { if (v.selected) selected.set(v.id, v.rank); });
  }

  const seriesNav = isInSeries ? `
    <div class="flex items-center gap-3 mb-3 text-sm">
      ${prevId
        ? `<button class="text-indigo-400 hover:text-indigo-300" onclick="navigateTo('/design/${_esc(prevId)}')">\u2190 Prev</button>`
        : `<span class="text-gray-600">\u2190 Prev</span>`}
      <span class="text-gray-400">Revision ${revisionIdx + 1} of ${revisionCount}</span>
      ${nextId
        ? `<button class="text-indigo-400 hover:text-indigo-300" onclick="navigateTo('/design/${_esc(nextId)}')">Next \u2192</button>`
        : `<span class="text-gray-600">Next \u2192</span>`}
    </div>` : '';

  // Populate header action buttons (sticky top nav)
  const headerActions = document.getElementById('header-actions');
  if (headerActions) {
    headerActions.innerHTML = `
      <span id="design-screenshot-status" class="text-xs text-gray-500"></span>
      <button onclick="manualCaptureScreenshot('${_esc(revisionId)}')"
              class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-500 hover:text-gray-300 hover:border-gray-500 transition-colors">
        Capture
      </button>
    `;
  }

  let html = `
    <div class="max-w-6xl mx-auto">
      <h2 class="text-xl font-bold text-indigo-400 mb-1">${_esc(exp.title)}</h2>
      ${seriesNav}
      ${exp.description ? `<p class="text-gray-400 text-sm mb-4">${_esc(exp.description)}</p>` : ''}
      ${isCompleted ? '<p class="text-green-400 text-sm mb-4 font-semibold">Results submitted</p>' : ''}
      <div id="design-variants">`;

  variants.forEach(v => {
    const isSelected = isCompleted && v.selected;
    html += `
        <div class="design-variant" data-variant-id="${_esc(v.id)}">
          <div class="design-variant-header">
            <span class="design-variant-label">${_esc(v.id)}</span>
            <div class="flex items-center gap-2">
              <span class="design-rank-wrap" style="display:none;">
                <label class="text-xs text-gray-400 mr-1">Rank</label>
                <select class="exp-rank-select" data-variant="${_esc(v.id)}" ${isCompleted ? 'disabled' : ''}>
                  ${variants.map((_, i) => `<option value="${i+1}">${i+1}</option>`).join('')}
                </select>
              </span>
              <button class="exp-select-btn ${isSelected ? 'selected' : ''}"
                      data-variant="${_esc(v.id)}" title="Select this variant"
                      ${isCompleted ? 'disabled' : ''}>
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3,8 7,12 13,4"/>
                </svg>
              </button>
            </div>
          </div>
          <iframe class="design-variant-iframe" data-variant="${_esc(v.id)}" style="min-height:300px;"></iframe>
        </div>`;
  });

  html += `</div>`;

  if (!isCompleted) {
    html += `
      <div class="exp-submit-bar" id="exp-submit-bar">
        <span class="text-sm text-gray-400" id="exp-selection-hint">Select variants to rank them</span>
        <button class="exp-submit-btn" id="exp-submit-btn" disabled onclick="submitExperiment('${revisionId}')">Submit Rankings</button>
      </div>`;
  }

  // Chat With terminal panel (hidden until spawned or reconnected)
  html += `
    <div id="chatwith-panel" class="mt-6 border border-gray-700 rounded overflow-hidden" style="display:none;">
      <div class="flex items-center px-3 py-2 bg-gray-800 border-b border-gray-700 cursor-pointer"
           onclick="toggleChatWithPanel()">
        <span class="text-sm font-semibold text-indigo-400">Chat With Claude</span>
        <span id="chatwith-status" class="text-xs text-gray-500 ml-2"></span>
        <div class="ml-auto flex items-center gap-2" onclick="event.stopPropagation()">
          <button id="chatwith-reconnect-btn"
                  onclick="connectChatWithTerminal('chatwith-${_esc(sessionCtx)}')"
                  class="text-xs text-indigo-400 hover:text-indigo-300 px-2 py-0.5 rounded border border-indigo-800 hover:border-indigo-600"
                  style="display:none;">Reconnect</button>
          <button id="chatwith-kill-btn"
                  onclick="killChatWithSession('${_esc(sessionCtx)}')"
                  class="text-xs text-red-400 hover:text-red-300 px-2 py-0.5 rounded border border-red-800 hover:border-red-600"
                  style="display:none;">Kill</button>
          <button id="chatwith-toggle-btn"
                  onclick="toggleChatWithPanel()"
                  class="text-xs text-gray-400 hover:text-white px-2">&#9662;</button>
        </div>
      </div>
      <div id="chatwith-body" style="height:300px;display:none;">
        <div id="chatwith-container" style="height:300px;"></div>
      </div>
    </div>`;

  html += `</div>`;
  _replaceFragment(content, html);

  // Auto-reconnect Chat With panel if session already exists (fire-and-forget).
  // For experiments in a series, also auto-show the panel so the user sees the
  // Chat With button without needing to scroll — one click starts the session.
  (async () => {
    const sessionName = `chatwith-${sessionCtx}`;
    try {
      const check = await api(`/api/chatwith/check?session=${encodeURIComponent(sessionName)}`);
      if (check && check.exists) {
        const btn = document.getElementById('chatwith-btn');
        if (btn) btn.textContent = 'Reconnect';
        connectChatWithTerminal(sessionName);
        // Active Chat With session — init display capture for screenshots
        initDisplayCapture(revisionId).catch(() => {});
      }
    } catch(e) { /* ignore — check is best-effort */ }
  })();

  // Inject fixture + HTML into iframes (with Tailwind + dashboard CSS so variants
  // use identical markup to the main app — winning variant drops in with zero rework)
  const _parentCSS = document.querySelector('style')?.textContent || '';
  let _iframeLoadCount = 0;
  let _screenshotTimer = null;
  variants.forEach(v => {
    const iframe = content.querySelector(`iframe[data-variant="${v.id}"]`);
    if (!iframe) return;
    const doc = iframe.contentDocument || iframe.contentWindow.document;
    // Wrap inline <script> bodies in a load listener so they execute
    // after Tailwind CDN has loaded and set up its MutationObserver.
    // Without this, variant JS that writes innerHTML with utility classes
    // runs before Tailwind sees the DOM, and classes are never processed.
    const _safeHtml = v.html.replace(
      /<script(?![^>]*\bsrc\b)([^>]*)>([\s\S]*?)<\/script>/gi,
      (_, attrs, body) => `<script${attrs}>window.addEventListener("load",function(){${body}});<\/script>`
    );
    doc.open();
    doc.write(`<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/tailwind.css">
<style>${_parentCSS}</style>
<style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#111827;color:#e5e7eb;}</style>
</head><body>
<script>window.FIXTURE = ${exp.fixture || '{}'};<\/script>
${_safeHtml}
</body></html>`);
    doc.close();

    // Auto-resize iframe to content height
    const resizeIframe = () => {
      try {
        const h = iframe.contentDocument.documentElement.scrollHeight;
        iframe.style.height = Math.max(200, Math.min(h, 800)) + 'px';
      } catch(e) {}
    };
    iframe.addEventListener('load', resizeIframe);
    // doc.write/doc.close doesn't fire 'load' reliably — use setTimeout
    setTimeout(resizeIframe, 200);
    setTimeout(resizeIframe, 600);
    _iframeLoadCount++;
    if (_iframeLoadCount >= variants.length) {
      if (_screenshotTimer) clearTimeout(_screenshotTimer);
      _screenshotTimer = setTimeout(async () => {
        // Try display stream capture first; fall back to iframe html2canvas (mobile)
        if (_displayStream) {
          await captureTabScreenshot(revisionId);
        } else {
          await _captureViaIframeHtml2Canvas(revisionId);
        }
      }, 1500);
    }
  });

  // Subscribe to design SSE topic so gallery auto-updates when a new revision is posted
  const designId = exp.design_id;
  if (designId) {
    const designTopic = `design:${designId}`;

    function _onNewDesignRevision(data) {
      // Ignore replays of the current revision
      const currentId = window.location.pathname.split('/design/')[1];
      if (!currentId || data.revision_id === currentId) return;
      // Only act if still on a design page (user may have navigated away)
      if (!window.location.pathname.startsWith('/design/')) return;
      navigateTo(`/design/${data.revision_id}`);
    }

    registerHandler(designTopic, _onNewDesignRevision);
    window._designSeriesCleanup = () => unregisterHandler(designTopic, _onNewDesignRevision);
  }

  // Display capture is initiated when Chat With spawns, not on page load.
  // If a stream is already active (from a previous Chat With), auto-capture.
  if (!isCompleted && _displayStream) {
    setTimeout(() => captureTabScreenshot(revisionId), 1500);
  }

  if (isCompleted) {
    // Show rank badges on completed variants
    variants.forEach(v => {
      if (v.selected && v.rank != null) {
        const rankWrap = content.querySelector(`[data-variant="${v.id}"]`)?.closest('.design-variant')?.querySelector('.exp-rank-wrap');
        if (rankWrap) {
          rankWrap.style.display = '';
          rankWrap.querySelector('.exp-rank-select').value = v.rank;
        }
      }
    });
    return;
  }

  // Selection toggle logic
  function updateSelectionUI() {
    const count = selected.size;
    const showRanks = count >= 2;
    content.querySelectorAll('.design-rank-wrap').forEach(el => {
      const vid = el.querySelector('.exp-rank-select').dataset.variant;
      el.style.display = (showRanks && selected.has(vid)) ? '' : 'none';
    });
    const hint = document.getElementById('exp-selection-hint');
    const btn = document.getElementById('exp-submit-btn');
    if (count === 0) {
      hint.textContent = 'Select variants to rank them';
      btn.disabled = true;
    } else if (count === 1) {
      hint.textContent = '1 selected \u2014 select more to rank, or submit as winner';
      btn.disabled = false;
    } else {
      hint.textContent = `${count} selected \u2014 set ranks and submit`;
      btn.disabled = false;
    }
  }

  content.querySelectorAll('.exp-select-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const vid = btn.dataset.variant;
      if (selected.has(vid)) {
        selected.delete(vid);
        btn.classList.remove('selected');
      } else {
        selected.set(vid, selected.size + 1);
        btn.classList.add('selected');
        // Set default rank
        const rankSel = content.querySelector(`.exp-rank-select[data-variant="${vid}"]`);
        if (rankSel) rankSel.value = selected.size;
      }
      updateSelectionUI();
    });
  });

  // Store selection map globally for submit
  window._expSelected = selected;
}

async function submitExperiment(revisionId) {
  const selected = window._expSelected;
  if (!selected || selected.size === 0) return;

  const btn = document.getElementById('exp-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Submitting...';

  // Gather selections with ranks from the UI
  const selections = [];
  selected.forEach((_, vid) => {
    const rankSel = document.querySelector(`.exp-rank-select[data-variant="${vid}"]`);
    const rank = rankSel ? parseInt(rankSel.value) : 1;
    selections.push({ id: vid, rank });
  });

  // If only 1 selected, rank is 1
  if (selections.length === 1) selections[0].rank = 1;

  const res = await fetch(`/api/design/${revisionId}/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selections }),
  });
  const data = await res.json();

  if (data.ok) {
    btn.textContent = 'Submitted';
    // Dismiss sidebar indicator for this design
    const indicator = document.querySelector(`[data-design-id="${revisionId}"]`);
    if (indicator) indicator.remove();
    // Refresh page to show completed state
    setTimeout(() => renderExperiment(revisionId), 500);
  } else {
    btn.disabled = false;
    btn.textContent = 'Submit Rankings';
    alert('Failed to submit: ' + (data.error || 'unknown error'));
  }
}

function _esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Sidebar Design Indicator ─────────────────────────────

async function dismissDesign(revisionId, designKey, triggerEl) {
  const sessionName = `chatwith-${designKey}`;
  const toastEl = triggerEl.closest('[data-design-id]');
  const hasChatWith = toastEl && toastEl.dataset.hasChatwith === 'true';
  const confirmMsg = hasChatWith ? 'End design and Chat With session?' : 'End?';

  const confirmed = await new Promise(resolve => {
    const confirm = document.createElement('span');
    confirm.className = 'design-dismiss-confirm';
    confirm.innerHTML = `${confirmMsg} <button class="design-dismiss-yes">Yes</button><button class="design-dismiss-no">No</button>`;
    triggerEl.replaceWith(confirm);
    confirm.querySelector('.design-dismiss-yes').onclick = (e) => { e.stopPropagation(); resolve(true); };
    confirm.querySelector('.design-dismiss-no').onclick = (e) => { e.stopPropagation(); confirm.replaceWith(triggerEl); resolve(false); };
  });
  if (!confirmed) return;
  try {
    await fetch(`/api/design/${revisionId}/dismiss`, { method: 'POST' });
  } catch (e) {
    console.warn('[dismiss] Failed to dismiss design:', e);
  }
  if (hasChatWith) {
    try {
      await fetch(`/api/terminal/${sessionName}/kill`);
    } catch (e) {
      console.warn('[dismiss] Failed to kill Chat With session:', e);
    }
  }
  const el = document.querySelector(`[data-design-id="${designKey}"]`);
  if (el) el.remove();
}


async function checkPendingDesigns() {
  const container = document.getElementById('sidebar-designs');
  if (!container) return;
  try {
    const [pending, chatData] = await Promise.all([
      api('/api/design/pending'),
      api('/api/chatwith/sessions').catch(() => ({ sessions: [] })),
    ]);
    // Build a mutable set of active chatwith session names; we'll delete matched ones
    // to find orphaned sessions afterward.
    const activeSessions = new Set((chatData && chatData.sessions) || []);
    const keys = new Set();

    // Detect if we're currently viewing a design page
    const currentDesignPath = window.location.pathname.startsWith('/design/')
      ? window.location.pathname.split('/design/')[1] : null;

    if (Array.isArray(pending)) {
      pending.forEach(des => {
        // Use design_id as the stable key so the toast represents the whole design
        const designKey = des.design_id || des.id;
        keys.add(designKey);
        const sessionName = `chatwith-${designKey}`;
        const hasChatWith = activeSessions.has(sessionName);
        // Consume session so it isn't treated as orphaned below
        activeSessions.delete(sessionName);

        // Check if this design is currently being viewed
        const isActive = currentDesignPath && (currentDesignPath === des.id || currentDesignPath === designKey);

        const iterLabel = des.iteration_count > 1
          ? `${_esc(des.title)} <span class="text-gray-500 text-xs">(${des.iteration_count} revisions)</span>`
          : _esc(des.title);
        const existing = container.querySelector(`[data-design-id="${designKey}"]`);
        if (existing) {
          // Update link target, label, and Chat With indicator
          existing.dataset.hasChatwith = hasChatWith ? 'true' : 'false';
          existing.href = `/design/${des.id}`;
          existing.onclick = (e) => { e.preventDefault(); navigateTo(`/design/${des.id}`); };
          existing.classList.toggle('sidebar-design-active', !!isActive);
          const textEl = existing.querySelector('.sidebar-design-text');
          if (textEl) textEl.innerHTML = iterLabel;
          // Sync pulsing dot
          let dot = existing.querySelector('.sidebar-design-chat-dot');
          if (hasChatWith && !dot) {
            dot = document.createElement('span');
            dot.className = 'sidebar-design-chat-dot';
            const icon = existing.querySelector('.sidebar-design-icon');
            existing.insertBefore(dot, icon ? icon.nextSibling : textEl);
          } else if (!hasChatWith && dot) {
            dot.remove();
          }
          return;
        }
        const link = document.createElement('a');
        link.className = 'sidebar-design' + (isActive ? ' sidebar-design-active' : '');
        link.dataset.designId = designKey;
        link.dataset.hasChatwith = hasChatWith ? 'true' : 'false';
        // Navigate to latest revision (des.id is already the latest from the API)
        link.href = `/design/${des.id}`;
        link.onclick = (e) => { e.preventDefault(); navigateTo(`/design/${des.id}`); };
        const chatDot = hasChatWith ? '<span class="sidebar-design-chat-dot"></span>' : '';
        link.innerHTML = `<span class="sidebar-design-icon">\uD83C\uDFA8</span>${chatDot}<span class="sidebar-design-text">${iterLabel}</span>`;
        const btn = document.createElement('button');
        btn.className = 'sidebar-design-dismiss';
        btn.title = 'Dismiss';
        btn.textContent = '\u00d7';
        btn.onclick = (e) => { e.preventDefault(); e.stopPropagation(); dismissDesign(des.id, designKey, btn); };
        link.appendChild(btn);
        container.appendChild(link);
      });
    }

    // Auto-kill orphaned chatwith/chat sessions — no parent design remains
    for (const sessionName of activeSessions) {
      if (!sessionName.startsWith('chatwith-') && !sessionName.startsWith('chat-')) continue;
      try {
        await fetch(`/api/terminal/${encodeURIComponent(sessionName)}/kill`, { method: 'POST' });
      } catch (_) {}
    }

    // Remove indicators for designs that are no longer pending (server is source of truth)
    container.querySelectorAll('[data-design-id]').forEach(el => {
      if (!keys.has(el.dataset.designId)) el.remove();
    });
  } catch(e) {}
}

// ── Plugin substrate ─────────────────────────────────────────
// `Autonomy.plugins` mirrors /api/plugins. The router asks
// `Autonomy.matchPlugin(path)` first; legacy if/else ladder runs only
// when no plugin claims the path. See bead auto-a79f6.

window.Autonomy = window.Autonomy || {};
window.Autonomy.plugins = [];

function _pluginScriptSrc(plugin) {
  const rev = plugin && plugin.asset_rev ? `?v=${encodeURIComponent(plugin.asset_rev)}` : '';
  return `/static/plugins/${plugin.id}/page.js${rev}`;
}

function _dropPluginFragmentCache(pluginId) {
  const prefix = `/pages/${pluginId}`;
  for (const key of _fragmentCache.keys()) {
    if (key === prefix || key.startsWith(prefix + '?')) {
      _fragmentCache.delete(key);
    }
  }
}

function _stampPluginAssetUrls(html, plugin) {
  if (!plugin || !plugin.asset_rev) return html;
  const base = `/static/plugins/${plugin.id}/`;
  const rev = `?v=${encodeURIComponent(plugin.asset_rev)}`;
  return html.replace(
    new RegExp(`${base.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\$&')}([^"'?#]+)(?!\\?v=)`, 'g'),
    `${base}$1${rev}`,
  );
}

window.Autonomy.refreshPlugins = async function () {
  try {
    const res = await fetch('/api/plugins');
    if (!res.ok) {
      window.Autonomy.plugins = [];
      return [];
    }
    const data = await res.json();
    window.Autonomy.plugins = Array.isArray(data.plugins) ? data.plugins : [];
    // Lazily inject any newly-enabled plugin's page.js so dynamically
    // toggled plugins (operator flips dashboard.plugin#1 mid-session)
    // get their Alpine factory loaded without a full page reload.
    // Awaiting the script's onload before returning prevents the
    // router from setting innerHTML + calling Alpine.initTree on a
    // fragment whose alpine_root isn't defined yet.
    const pending = [];
    for (const p of window.Autonomy.plugins) {
      const existing = document.querySelector(`script[data-plugin-id="${p.id}"]`);
      if (existing && existing.dataset.assetRev === (p.asset_rev || '')) {
        continue;
      }
      if (existing) {
        existing.remove();
        _dropPluginFragmentCache(p.id);
      }
      const script = document.createElement('script');
      script.src = _pluginScriptSrc(p);
      script.dataset.pluginId = p.id;
      script.dataset.assetRev = p.asset_rev || '';
      pending.push(new Promise(resolve => {
        script.onload = resolve;
        script.onerror = resolve;
      }));
      document.body.appendChild(script);
    }
    if (pending.length) {
      await Promise.all(pending);
    }
    return window.Autonomy.plugins;
  } catch (e) {
    window.Autonomy.plugins = [];
    return [];
  }
};

window.Autonomy.matchPlugin = function (path) {
  const list = window.Autonomy.plugins || [];
  for (const p of list) {
    if (path === p.path || path.startsWith(p.path + '/')) {
      return p;
    }
  }
  return null;
};

function _renderSidebarPlugins() {
  const slot = document.getElementById('sidebar-plugins');
  if (!slot) return;
  slot.innerHTML = '';
  for (const p of window.Autonomy.plugins || []) {
    const a = document.createElement('a');
    a.href = p.path;
    a.className = 'nav-link';
    a.dataset.page = p.id;
    a.setAttribute('onclick', 'closeSidebar()');
    a.textContent = p.label + ' ';
    const badge = document.createElement('span');
    badge.id = `badge-plugin-${p.id}`;
    badge.dataset.testid = `badge-plugin-${p.id}`;
    badge.className = `nav-badge nav-badge-${p.badge_color || 'gray'}`;
    a.appendChild(badge);
    slot.appendChild(a);
  }
}

async function renderPluginFragment(plugin) {
  pageTitle.textContent = plugin.label;
  // Stamp the plugin's effective org so subsequent api() / Autonomy.fetch
  // calls from inside the page carry X-Graph-Org. The fragment fetch
  // itself goes through the wrapper too so the page shell is scoped.
  window.Autonomy._activePluginOrg = plugin.org || null;
  // Stamp the plugin id so Schema.alpine() (and other plugin-aware
  // helpers) can self-identify without the shell threading it through.
  window.Autonomy._activePluginId = plugin.id || null;
  const fragmentUrl = plugin.asset_rev
    ? `/pages/${plugin.id}?v=${encodeURIComponent(plugin.asset_rev)}`
    : `/pages/${plugin.id}`;
  let html;
  if (_fragmentCache.has(fragmentUrl)) {
    html = _fragmentCache.get(fragmentUrl);
  } else {
    const res = await fetch(fragmentUrl, _withOrgHeader());
    if (!res.ok) {
      _replaceFragment(content, '<div class="text-gray-400">Page not found</div>');
      return;
    }
    html = await res.text();
    html = _stampPluginAssetUrls(html, plugin);
    _fragmentCache.set(fragmentUrl, html);
  }
  _replaceFragment(content, html);
}

// ── Router ───────────────────────────────────────────────────

function navigateTo(path) {
  if (path === window.location.pathname + window.location.search) return;
  history.replaceState({ scrollY: window.scrollY }, '');
  history.pushState({}, '', path);
  route();
}

async function route() {
  // Drop any plugin-org context from the previous render so non-plugin
  // routes (and plugin pages whose load_enabled state changed) don't
  // inherit a stale X-Graph-Org. The plugin handler resets it below
  // when the new path matches a plugin.
  window.Autonomy._activePluginOrg = null;
  window.Autonomy._activePluginId = null;
  await _checkVersion();
  await window.Autonomy.refreshPlugins();
  _renderSidebarPlugins();
  const path = window.location.pathname;
  const isTerminalPage = path === '/terminal' || path.startsWith('/terminal/');
  const isSessionViewPage = /^\/session\/[^/]+\/.+$/.test(path);

  // Toggle between #content and persistent #terminal-page
  const termPage = document.getElementById('terminal-page');
  if (isTerminalPage) {
    content.style.display = 'none';
    termPage.style.display = '';
  } else {
    termPage.style.display = 'none';
    content.style.display = '';
  }

  // Hide global header on design pages (control strip replaces it)
  const isDesignPage = path.startsWith('/design/');
  const globalHeader = document.querySelector('header');
  if (globalHeader) {
    globalHeader.style.display = isDesignPage ? 'none' : '';
  }
  // Remove content padding for full-bleed design
  content.style.padding = isDesignPage ? '0' : '';

  // Fullscreen page mode: session viewer owns the viewport (hides sidebar + header)
  document.body.classList.toggle('fullscreen-page', isSessionViewPage);

  // Per-route body class — currently only used by /search to flush its
  // sticky filter strip against the global header (drops the 24px gap
  // caused by main's pt-6 baseline). See bead auto-kvka6 §7.
  document.body.classList.toggle('route-search', path === '/search');

  // Clear header action buttons from previous page
  const headerActions = document.getElementById('header-actions');
  if (headerActions) headerActions.innerHTML = '';

  // Clear any auto-refresh intervals from previous page (managed by Alpine lifecycle)

  // Update active nav
  document.querySelectorAll('.nav-link').forEach(el => {
    const raw = el.dataset.activeMatch || el.dataset.page || '';
    const prefixes = raw.split(',').map(s => s.trim()).filter(Boolean);
    const isActive = prefixes.some(prefix => (
      path === '/' + prefix || path.startsWith('/' + prefix + '/')
    ));
    el.classList.toggle('active', isActive);
  });

  // Update global search placeholder based on page
  globalSearch.placeholder = (path === '/' || path === '/beads') ? 'Search beads...'
    : path === '/sessions' ? 'Search sessions...'
    : path === '/worktrees' ? 'Search worktrees...'
    : path === '/streams' ? 'Search streams...'
    : 'Search graph...';

  // Plugin-aware routing — check the substrate before the legacy ladder.
  const matchedPlugin = window.Autonomy.matchPlugin(path);
  if (matchedPlugin) {
    renderPluginFragment(matchedPlugin);
  } else if (path === '/' || path === '/beads') {
    renderBeadsFragment();
  } else if (path.startsWith('/dispatch/trace/')) {
    renderTraceFragment();
  } else if (path === '/dispatch' || path === '/dispatch/alpine' || path === '/dispatch/lit') {
    renderDispatchFragment();
  } else if (path.startsWith('/bead/')) {
    renderBeadDetailFragment(path.split('/bead/')[1]);
  } else if (path === '/timeline' || path === '/activity') {
    renderTimelineFragment();
  } else if (path === '/sessions') {
    renderSessionsFragment();
  } else if (path === '/worktrees') {
    renderWorktreesFragment();
  } else if (path.match(/^\/session\/[^/]+\/.+$/)) {
    renderSessionViewFragment();
  } else if (path === '/collab') {
    renderCollabFragment();
  } else if (path === '/streams') {
    renderStreamsFragment();
  } else if (path.startsWith('/stream/')) {
    renderStreamFragment();
  } else if (path.startsWith('/graph/') || path.startsWith('/source/')) {
    renderSourceFragment();
  } else if (isTerminalPage) {
    const sessionId = path.startsWith('/terminal/') ? path.split('/terminal/')[1] : null;
    renderTerminal(null, sessionId);
  } else if (path.startsWith('/design/')) {
    renderDesignFragment();
  } else if (path === '/search') {
    renderSearchFragment();
  } else {
    _replaceFragment(content, '<div class="text-gray-400">Page not found</div>');
  }
  // Notify persistent (un-cleared) header components that the SPA route
  // changed. Used by the agent-actions dropdown — its Alpine root lives
  // outside the per-page fragment so x-init runs once; this event lets
  // it re-derive its asset and refetch members on every nav.
  window.dispatchEvent(new CustomEvent('app:navigated', {
    detail: { path: window.location.pathname },
  }));
}

// ── Event Handlers ───────────────────────────────────────────

// Global search — context-sensitive.
//   - On /search: broadcast every input change as a ``global-search:input``
//     CustomEvent so the search-page Alpine component can two-way bind to
//     ``query``, debounce a refetch, and replaceState() the q= in the URL.
//     Enter dispatches ``global-search:enter`` so the page can flush its
//     debounce immediately. The page is the listener; this code stays
//     route-agnostic.
//   - On /beads: the Alpine component listens to native input events on
//     #global-search directly.
//   - Anywhere else: Enter navigates to /search?q=…
globalSearch.addEventListener('input', () => {
  if (window.location.pathname === '/search') {
    window.dispatchEvent(new CustomEvent('global-search:input', {
      detail: { value: globalSearch.value },
    }));
  }
});
globalSearch.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const q = globalSearch.value.trim();
  const path = window.location.pathname;
  if (path === '/search') {
    window.dispatchEvent(new CustomEvent('global-search:enter', {
      detail: { value: globalSearch.value },
    }));
    return;
  }
  if (path === '/' || path === '/beads') {
    // On beads page: Alpine component reacts to input events — no extra action needed
    return;
  }
  if (!q) return;
  navigateTo('/search?q=' + encodeURIComponent(q));
});

// Client-side nav (no full page reload)
document.addEventListener('click', (e) => {
  if (e.defaultPrevented) return;        // another handler already handled this click
  const link = e.target.closest('a[href]');
  if (!link) return;
  // Hash-only links (in-page anchors): let the browser handle native scroll +
  // hash update. Routing through navigateTo would strip the hash and then
  // short-circuit on the matching pathname.
  const rawHref = link.getAttribute('href') || '';
  if (rawHref.startsWith('#')) return;
  if (link.hasAttribute('data-hard-reload')) return;
  if (link.origin === window.location.origin && !link.pathname.startsWith('/api/') && !link.hasAttribute('download')) {
    e.preventDefault();
    navigateTo(link.pathname + link.search);
  }
});

window.addEventListener('popstate', (e) => {
  route();
  if (e.state && e.state.scrollY !== undefined) {
    requestAnimationFrame(() => window.scrollTo(0, e.state.scrollY));
  }
});

// ── Init ─────────────────────────────────────────────────────

// Live dispatch badge via SSE nav topic
connectEvents(['nav', 'dispatch'], {
  dispatch: () => {},  // cache-only — Alpine component handles rendering
  nav: (data) => {
    const running = data.running_agents || 0;
    const waiting = data.approved_waiting || 0;
    const blocked = data.approved_blocked || 0;

    const dispatchEl = document.getElementById('badge-dispatch');
    if (dispatchEl) {
      let html = '';
      if (running) html += `<span class="nav-badge nav-badge-green">▶${running}</span>`;
      if (waiting) html += `<span class="nav-badge nav-badge-blue">◦${waiting}</span>`;
      if (blocked) html += `<span class="nav-badge nav-badge-amber">⊘${blocked}</span>`;
      dispatchEl.innerHTML = html;
    }

    const worktreesEl = document.getElementById('badge-worktrees');
    if (worktreesEl) {
      const withCommits = data.worktrees_with_commits || 0;
      const withChanges = data.worktrees_with_changes || 0;
      let html = '';
      if (withCommits) html += `<span class="nav-badge nav-badge-green">${withCommits}</span>`;
      if (withChanges) html += `<span class="nav-badge nav-badge-amber">${withChanges}</span>`;
      worktreesEl.innerHTML = html;
    }

    const beadsEl = document.getElementById('badge-beads');
    if (beadsEl && data.open_beads != null) beadsEl.textContent = data.open_beads || '';

    const sessionsEl = document.getElementById('badge-sessions');
    if (sessionsEl) sessionsEl.textContent = data.active_sessions || '';

    const activityEl = document.getElementById('badge-activity');
    if (activityEl) activityEl.textContent = data.today_done || '';

    const terminalEl = document.getElementById('badge-terminal');
    if (terminalEl) terminalEl.textContent = data.terminal_count || '';

    const streamsEl = document.getElementById('badge-streams');
    if (streamsEl) streamsEl.textContent = data.stream_count || '';

    // Update pinned beads strip
    if (data.pinned && window.Alpine) {
      Alpine.store('pinned').beads = data.pinned;
    }
    if (_harnessUsageMode !== 'settings' && data.harness_usage) {
      renderHarnessUsage(data.harness_usage);
    }
  },
});

// Load stats — compact 2x2 grid
api('/api/stats').then(data => {
  const raw = data.results || '';
  // Parse "table  count" lines into key/value pairs
  const entries = [];
  raw.split('\n').forEach(line => {
    const m = line.match(/^\s*(\w+)\s+(\d+)/);
    if (m) entries.push([m[1], parseInt(m[2])]);
  });
  // Show the 4 most important: sources, thoughts, entities, edges
  const keys = ['sources', 'thoughts', 'entities', 'edges'];
  const show = keys.map(k => {
    const e = entries.find(([name]) => name === k);
    return e ? e : [k, 0];
  });
  if (show.length) {
    const fmt = n => n >= 1000 ? (n/1000).toFixed(1) + 'k' : String(n);
    statsSummary.innerHTML = '<div class="kb-stats">' +
      show.map(([k,v]) =>
        `<div class="kb-stat"><span class="kb-stat-label">${k}</span><span class="kb-stat-value">${fmt(v)}</span></div>`
      ).join('') + '</div>';
  } else {
    statsSummary.textContent = raw.trim() ? raw.trim().split('\n').slice(0, 2).join(', ') : '';
  }
});

initHarnessUsageSettings();

// connectEvents() is defined in static/js/events.js (loaded before this file).

// Check for pending designs
checkPendingDesigns();
setInterval(checkPendingDesigns, 10000);

// ── Toast notifications ──────────────────────────────────────

// ``fatal``-class toasts escalate to a blocking full-screen modal
// with an explicit Refresh button. Used when the page is in an
// unrecoverable state (e.g. initial load failed and no data reached
// the surface) — a regular toast scrolls away and leaves the page
// silently broken. The modal stays put until the operator acts.
function _showFatalModal(message) {
  // De-dupe: a second fatal call updates the existing message rather
  // than stacking modals on top of each other.
  const existing = document.getElementById('fatal-modal-backdrop');
  if (existing) {
    const msgEl = existing.querySelector('.fatal-modal-message');
    if (msgEl) msgEl.textContent = message || '';
    return;
  }
  const backdrop = document.createElement('div');
  backdrop.id = 'fatal-modal-backdrop';
  backdrop.className = 'fatal-modal-backdrop';
  backdrop.setAttribute('role', 'alertdialog');
  backdrop.setAttribute('aria-modal', 'true');
  backdrop.setAttribute('aria-labelledby', 'fatal-modal-title');
  backdrop.setAttribute('data-testid', 'fatal-modal');

  const card = document.createElement('div');
  card.className = 'fatal-modal-card';

  const title = document.createElement('div');
  title.id = 'fatal-modal-title';
  title.className = 'fatal-modal-title';
  title.textContent = 'Needs refresh';

  const msg = document.createElement('div');
  msg.className = 'fatal-modal-message';
  msg.setAttribute('data-testid', 'fatal-modal-message');
  msg.textContent = message || '';

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'fatal-modal-button';
  button.setAttribute('data-testid', 'fatal-modal-refresh');
  button.textContent = 'Refresh';
  button.addEventListener('click', () => {
    if (typeof window !== 'undefined' && window.location
        && typeof window.location.reload === 'function') {
      window.location.reload();
    }
  });

  card.appendChild(title);
  card.appendChild(msg);
  card.appendChild(button);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);
  // Focus the Refresh button so Enter/Space resolves the modal
  // without forcing the operator to mouse over to it.
  if (typeof button.focus === 'function') {
    try { button.focus(); } catch (_) { /* not focusable in test */ }
  }
}

function showToast(message, type) {
  if (type === 'fatal') {
    _showFatalModal(message);
    return;
  }
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast toast-' + (type || 'error');
  el.textContent = message;
  el.onclick = () => {
    el.style.animation = 'toast-out 0.2s ease-in forwards';
    el.addEventListener('animationend', () => el.remove());
  };
  container.appendChild(el);
  setTimeout(() => {
    if (el.parentNode) {
      el.style.animation = 'toast-out 0.2s ease-in forwards';
      el.addEventListener('animationend', () => el.remove());
    }
  }, 8000);
}

// Expose the modal renderer for tests and any caller that wants to
// trigger the fatal flow without going through showToast.
if (typeof window !== 'undefined') {
  window.showToast = showToast;
  window.showFatalModal = _showFatalModal;
}

// ── Dispatcher state watcher (global, all pages) ─────────────

(function () {
  let _prevPaused = null;
  registerHandler('dispatcher_state', function (state) {
    const nowPaused = !!state.paused;
    // Detect false→true transition (skip first load if already paused)
    if (_prevPaused === false && nowPaused && state.reason) {
      const reason = state.reason;
      let msg = 'Dispatcher paused';
      if (reason.reason === 'auth') {
        msg = 'Dispatcher paused: authentication failed';
      } else if (reason.message) {
        msg = 'Dispatcher paused: ' + reason.message;
      }
      showToast(msg, 'error');
    }
    _prevPaused = nowPaused;
  });
})();

// Initial plugin script injection — fetched once at boot. Each enabled
// plugin's page.js is loaded as a <script> tag, so its
// `Alpine.data('<alpine_root>', ...)` factory is registered before the
// router renders any plugin fragment.
(async () => {
  const plugins = await window.Autonomy.refreshPlugins();
  for (const p of plugins) {
    const existing = document.querySelector(`script[data-plugin-id="${p.id}"]`);
    if (existing && existing.dataset.assetRev === (p.asset_rev || '')) continue;
    if (existing) existing.remove();
    const script = document.createElement('script');
    script.src = _pluginScriptSrc(p);
    script.dataset.pluginId = p.id;
    script.dataset.assetRev = p.asset_rev || '';
    document.body.appendChild(script);
  }
  _renderSidebarPlugins();
  route();
})();
