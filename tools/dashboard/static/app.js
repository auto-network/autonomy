// Autonomy Dashboard — client-side routing and rendering
// Every view is a function that fetches JSON from the API and renders it.

const content = document.getElementById('content');
const pageTitle = document.getElementById('page-title');
const statsSummary = document.getElementById('stats-summary');
const harnessUsage = document.getElementById('harness-usage');
const globalSearch = document.getElementById('global-search');
const globalSearchIcon = document.getElementById('global-search-icon');
const appTopbarSlot = document.getElementById('app-topbar-slot');
const sessionViewLayer = document.getElementById('session-view-layer');
const sessionViewHost = document.getElementById('session-view-host');
const HARNESS_USAGE_SETTINGS_SET_ID = 'dashboard.harness.usage';
const HARNESS_USAGE_STALE_MS = 15 * 60 * 1000;
let _currentContentPath = null;
let _sessionOverlayBasePath = null;

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

function _escapeTopbarHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function _setTopbarHtml(opts) {
  const header = document.querySelector('header');
  if (!header || !appTopbarSlot) return;
  appTopbarSlot.innerHTML = opts && opts.html ? opts.html : '';
  header.classList.add('app-topbar-active');
  header.classList.toggle('app-topbar-has-search', !!(opts && opts.hasSearch));
}

window.Autonomy.resetTopbar = function () {
  if (window.Autonomy._activeTopbarHandle
      && typeof window.Autonomy._activeTopbarHandle.destroy === 'function') {
    const handle = window.Autonomy._activeTopbarHandle;
    window.Autonomy._activeTopbarHandle = null;
    handle.destroy();
  }
  const header = document.querySelector('header');
  if (header) {
    header.classList.remove('app-topbar-active');
    header.classList.remove('app-topbar-has-search');
  }
  if (appTopbarSlot) appTopbarSlot.innerHTML = '';
};

window.Autonomy.setTopbar = function (opts) {
  if (window.Autonomy._activeTopbarHandle
      && typeof window.Autonomy._activeTopbarHandle.destroy === 'function') {
    const handle = window.Autonomy._activeTopbarHandle;
    window.Autonomy._activeTopbarHandle = null;
    handle.destroy();
  }
  _setTopbarHtml(opts || {});
  return {
    update(nextOpts) { window.Autonomy.setTopbar(nextOpts || opts || {}); },
    destroy() {
      const header = document.querySelector('header');
      if (header) header.classList.remove('app-topbar-has-search');
      if (appTopbarSlot) appTopbarSlot.innerHTML = '';
    },
  };
};

window.Autonomy.topbar = window.Autonomy.topbar || {};
window.Autonomy.topbar.set = function (initialOptions) {
  let options = Object.assign({}, initialOptions || {});
  const state = { searchOpen: {} };
  let cleanupFns = [];
  let destroyed = false;

  function cleanupBindings() {
    cleanupFns.forEach(fn => fn());
    cleanupFns = [];
  }

  function allControls() {
    return []
      .concat(options.left || [])
      .concat(options.controls || []);
  }

  function searchControls() {
    return allControls().filter(c => c && c.type === 'search');
  }

  function controlId(control, index) {
    return String(control.id || `${control.type || 'control'}-${index}`);
  }

  function optionRows(control) {
    return (control.options || []).map(opt => {
      const value = typeof opt === 'string' ? opt : opt.value;
      const label = typeof opt === 'string' ? opt : (opt.label || opt.value);
      const selected = String(value) === String(control.value) ? ' selected' : '';
      return '<option value="' + _escapeTopbarHtml(value) + '"' + selected + '>'
        + _escapeTopbarHtml(label) + '</option>';
    }).join('');
  }

  function searchIconSvg() {
    return '<svg aria-hidden="true" viewBox="0 0 24 24" width="15" height="15"'
      + ' fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round">'
      + '<circle cx="11" cy="11" r="8"></circle>'
      + '<path d="m21 21-4.35-4.35"></path>'
      + '</svg>';
  }

  function renderControl(control, index, region) {
    if (!control || typeof control !== 'object') return '';
    const id = controlId(control, index);
    const domId = `app-topbar-${region}-${id}`;
    if (control.type === 'html') {
      return '<span class="app-topbar-control app-topbar-html-control"'
        + ' data-topbar-control-id="' + _escapeTopbarHtml(id) + '">'
        + (control.html || '') + '</span>';
    }
    if (control.type === 'select') {
      const label = control.label
        ? '<label for="' + _escapeTopbarHtml(domId) + '">'
          + _escapeTopbarHtml(control.label) + '</label>'
        : '';
      return '<span class="app-topbar-control app-topbar-select-control"'
        + ' data-topbar-control-id="' + _escapeTopbarHtml(id) + '">'
        + label
        + '<select id="' + _escapeTopbarHtml(domId) + '"'
        + ' class="app-topbar-select" data-testid="' + _escapeTopbarHtml(control.testId || id) + '">'
        + optionRows(control)
        + '</select></span>';
    }
    if (control.type === 'search') {
      const open = !!state.searchOpen[id];
      const actionLabel = open
        ? (control.submitLabel || 'Apply search')
        : (control.openLabel || 'Open search');
      return '<span class="app-topbar-control app-topbar-search-control'
        + (open ? ' is-open' : '') + '"'
        + ' data-topbar-control-id="' + _escapeTopbarHtml(id) + '">'
        + '<input id="' + _escapeTopbarHtml(domId) + '"'
        + ' class="app-topbar-search-input"'
        + ' data-testid="' + _escapeTopbarHtml(control.testId || id) + '"'
        + ' type="text" value="' + _escapeTopbarHtml(control.value || '') + '"'
        + ' aria-hidden="' + (open ? 'false' : 'true') + '"'
        + ' tabindex="' + (open ? '0' : '-1') + '"'
        + ' placeholder="' + _escapeTopbarHtml(control.placeholder || 'Search') + '">'
        + '<button type="button" class="app-topbar-search-button"'
        + ' data-topbar-search-action="' + _escapeTopbarHtml(id) + '"'
        + ' data-testid="' + _escapeTopbarHtml((control.testId || id) + '-toggle') + '"'
        + ' aria-expanded="' + (open ? 'true' : 'false') + '"'
        + ' aria-controls="' + _escapeTopbarHtml(domId) + '"'
        + ' aria-label="' + _escapeTopbarHtml(actionLabel) + '"'
        + ' title="' + _escapeTopbarHtml(actionLabel) + '">'
        + searchIconSvg()
        + '</button>'
        + '</span>';
    }
    if (control.type === 'button') {
      return '<button type="button" class="app-topbar-button"'
        + ' data-topbar-control-id="' + _escapeTopbarHtml(id) + '"'
        + ' data-testid="' + _escapeTopbarHtml(control.testId || id) + '">'
        + _escapeTopbarHtml(control.label || id) + '</button>';
    }
    return '';
  }

  function renderRegion(regionControls, region) {
    return (regionControls || [])
      .map((control, index) => renderControl(control, index, region))
      .join('');
  }

  function html() {
    const subtitle = options.subtitle
      ? '<span>' + _escapeTopbarHtml(options.subtitle) + '</span>'
      : '';
    const left = renderRegion(options.left || [], 'left');
    const controls = renderRegion(options.controls || [], 'controls');
    return '<div class="app-structured-topbar" data-testid="app-structured-topbar">'
      + '<div class="app-topbar-main">'
      + '<div class="app-topbar-title"><strong>'
      + _escapeTopbarHtml(options.title || '')
      + '</strong>' + subtitle + '</div>'
      + (left ? '<div class="app-topbar-left">' + left + '</div>' : '')
      + '</div>'
      + '<div class="app-topbar-controls">' + controls + '</div>'
      + '</div>';
  }

  function bindControls() {
    allControls().forEach((control, index) => {
      if (!control || typeof control !== 'object') return;
      const id = controlId(control, index);
      const escapedId = window.CSS && typeof CSS.escape === 'function'
        ? CSS.escape(id)
        : id.replace(/["\\]/g, '\\$&');
      const root = appTopbarSlot && appTopbarSlot.querySelector(
        `[data-topbar-control-id="${escapedId}"]`
      );
      if (control.type === 'html' && root && typeof control.onMount === 'function') {
        control.onMount(root);
      }
      if (control.type === 'button' && root && typeof control.onClick === 'function') {
        root.onclick = event => control.onClick(event);
      }
      if (control.type === 'select' && root) {
        const select = root.querySelector('select');
        if (select && typeof control.onChange === 'function') {
          select.onchange = event => control.onChange(event.target.value, event);
        }
      }
      if (control.type === 'search' && root) {
        const input = root.querySelector('input');
        const action = root.querySelector('[data-topbar-search-action]');
        const isEmptySearch = () => String(control.value || '').trim() === '';
        const collapseIfEmpty = () => {
          if (!state.searchOpen[id] || !isEmptySearch()) return;
          state.searchOpen[id] = false;
          render();
        };
        const submitSearch = event => {
          if (typeof control.onSubmit === 'function') {
            control.onSubmit(control.value || '', event);
          } else if (input) {
            input.focus({ preventScroll: true });
          }
        };
        root.onfocusout = () => {
          setTimeout(() => {
            if (!root.contains(document.activeElement)) {
              collapseIfEmpty();
            }
          }, 0);
        };
        const onOutsidePointer = event => {
          if (!state.searchOpen[id] || root.contains(event.target)) return;
          setTimeout(collapseIfEmpty, 0);
        };
        document.addEventListener('pointerdown', onOutsidePointer, true);
        cleanupFns.push(() => {
          document.removeEventListener('pointerdown', onOutsidePointer, true);
        });
        if (input) {
          input.oninput = event => {
            control.value = event.target.value;
            if (typeof control.onInput === 'function') {
              control.onInput(event.target.value, event);
            }
          };
          input.onkeydown = event => {
            if (event.key === 'Escape') {
              event.preventDefault();
              state.searchOpen[id] = false;
              control.value = '';
              if (typeof control.onInput === 'function') control.onInput('', event);
              render();
              return;
            }
            if (event.key === 'Enter') {
              event.preventDefault();
              submitSearch(event);
            }
          };
          if (state.searchOpen[id] && control.autoFocus !== false) {
            setTimeout(() => input.focus({ preventScroll: true }), 0);
          }
        }
        if (action) {
          action.onclick = event => {
            if (!state.searchOpen[id]) {
              state.searchOpen[id] = true;
              render();
              return;
            }
            submitSearch(event);
          };
        }
      }
    });
  }

  function render() {
    if (destroyed) return;
    cleanupBindings();
    _setTopbarHtml({ html: html(), hasSearch: false });
    bindControls();
  }

  const handle = {
    update(nextOptions) {
      options = Object.assign({}, options, nextOptions || {});
      render();
      return handle;
    },
    destroy() {
      destroyed = true;
      cleanupBindings();  // always release our own control bindings
      // Only touch the shared topbar DOM if we still own it. A stale handle
      // destroying after another page has taken the topbar (via topbar.set)
      // must not blank the destination page's slot.
      if (window.Autonomy._activeTopbarHandle === handle) {
        const header = document.querySelector('header');
        if (header) header.classList.remove('app-topbar-has-search');
        if (appTopbarSlot) appTopbarSlot.innerHTML = '';
        window.Autonomy._activeTopbarHandle = null;
      }
    },
    openSearch(id) {
      const controls = searchControls();
      const all = allControls();
      const target = id || (
        controls[0] && controlId(controls[0], all.indexOf(controls[0]))
      );
      if (!target) return false;
      state.searchOpen[target] = true;
      render();
      return true;
    },
  };

  if (window.Autonomy._activeTopbarHandle
      && typeof window.Autonomy._activeTopbarHandle.destroy === 'function') {
    window.Autonomy._activeTopbarHandle.destroy();
  }
  window.Autonomy._activeTopbarHandle = handle;
  render();
  return handle;
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

  // Chevron nav (auto-j0udw) — desktop affordance for mouse users.
  // Buttons only render when hasMulti so we don't need a count check.
  const goToPage = (pageIndex) => {
    const clamped = clampHarnessUsagePage(pageIndex, items.length);
    if (clamped !== _harnessUsagePage) {
      _harnessUsagePage = clamped;
      updateHarnessUsageState(items);
      syncHarnessUsageScroll('smooth');
    }
  };
  harnessUsage.querySelectorAll('.harness-strip-nav').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const direction = Number(btn.dataset.direction || 0) || 0;
      goToPage(_harnessUsagePage + direction);
    });
  });

  // Dot clicks (auto-j0udw) — wire .harness-strip-dot[data-index] so
  // operators can jump directly to a page. Dots already render with
  // the data-index attribute; this just hooks the click handler.
  harnessUsage.querySelectorAll('.harness-strip-dot').forEach((dot) => {
    dot.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      goToPage(Number(dot.dataset.index || 0));
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
  const hasMulti = items.length > 1;
  const navAria = (label) => `aria-label="${_esc(label)}"`;
  harnessUsage.innerHTML = `
    <div class="harness-strip${hasMulti ? ' has-multi' : ''}">
      ${hasMulti ? `<button type="button" class="harness-strip-nav harness-strip-nav-prev" data-direction="-1" ${navAria('Previous identity')}>&lsaquo;</button>` : ''}
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
      ${hasMulti ? `<button type="button" class="harness-strip-nav harness-strip-nav-next" data-direction="1" ${navAria('Next identity')}>&rsaquo;</button>` : ''}
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
  // Claude credential/usage rows are host-local — always read from personal.db,
  // independent of the dashboard shell's org, matching the poller/launcher.
  const members = await _harnessUsageSchema.all({ headers: { 'X-Graph-Org': 'personal' } });
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
  _currentContentPath = '/sessions';
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

async function renderSessionViewFragment(host) {
  pageTitle.textContent = 'Session';
  const target = host || content;
  let html;
  if (_fragmentCache.has('/pages/session-view')) {
    html = _fragmentCache.get('/pages/session-view');
  } else {
    const res = await fetch('/pages/session-view');
    html = await res.text();
    _fragmentCache.set('/pages/session-view', html);
  }
  _replaceFragment(target, html);
  if (target === content) _currentContentPath = window.location.pathname;
}

// A bare ``/session/<name>`` URL (no project segment) can only be resolved
// server-side — the project lives in the live tmux table or the graph
// source's metadata, neither of which the client carries. Client-side
// pushState nav never consults the server, so these links used to
// dead-end at "Page not found"; only a hard refresh worked, because the
// server's ``page_session_view_by_name`` route 302-redirects the bare
// form to ``/session/<project>/<name>``. Mirror that here so in-app nav
// behaves exactly like a refresh: ask the server to resolve (HEAD +
// follow the redirect), adopt the canonical two-segment URL, then re-run
// the router so the viewer renders with the correct body classes. Covers
// the source-view session chip AND journal-entry links, both of which
// emit bare ``/session/<name>`` URLs.
async function resolveBareSessionPath(path) {
  try {
    const res = await fetch(path, { method: 'HEAD' });
    if (res.redirected) {
      const u = new URL(res.url);
      if (_isSessionPath(u.pathname)) {
        history.replaceState({}, '', u.pathname + u.search);
        route();
        return;
      }
    }
  } catch (_e) { /* fall through to not-found */ }
  _replaceFragment(content, '<div class="text-gray-400">Session not found</div>');
}

function _isSessionPath(path) {
  return /^\/session\/[^/]+\/.+$/.test(path || '');
}

function _isMobileOverlayViewport() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(max-width: 767px)').matches;
}

function _sessionOverlayCanHandle(path) {
  return !!(
    sessionViewLayer
    && sessionViewHost
    && _isMobileOverlayViewport()
    && _isSessionPath(path)
    && _currentContentPath === '/sessions'
  );
}

function _showSessionOverlayChrome() {
  if (!sessionViewLayer) return;
  sessionViewLayer.classList.add('active');
  sessionViewLayer.setAttribute('aria-hidden', 'false');
  document.body.classList.add('session-overlay-active');
  if (content) {
    if ('inert' in content) content.inert = true;
    content.setAttribute('aria-hidden', 'true');
  }
}

function _hideSessionOverlayChrome() {
  if (!sessionViewLayer) return;
  sessionViewLayer.classList.remove('active');
  sessionViewLayer.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('session-overlay-active');
  if (content) {
    if ('inert' in content) content.inert = false;
    content.removeAttribute('aria-hidden');
  }
}

function _closeSessionOverlayToList() {
  const basePath = _sessionOverlayBasePath || '/sessions';
  if (sessionViewLayer && sessionViewLayer.classList.contains('active')) {
    _hideSessionOverlayChrome();
    if (window.location.pathname !== basePath) {
      history.replaceState({ scrollY: window.scrollY }, '', basePath);
    }
    pageTitle.textContent = 'Sessions';
    window.dispatchEvent(new CustomEvent('app:navigated', {
      detail: { path: window.location.pathname },
    }));
    return true;
  }
  navigateTo('/sessions');
  return true;
}

window.Autonomy.closeSessionOverlayToList = _closeSessionOverlayToList;

function _handleSessionOverlayPopstate(e) {
  const basePath = _sessionOverlayBasePath || '/sessions';
  if (
    !sessionViewLayer ||
    !sessionViewLayer.classList.contains('active') ||
    window.location.pathname !== basePath
  ) {
    return false;
  }
  _closeSessionOverlayToList();
  if (e && e.state && e.state.scrollY !== undefined) {
    requestAnimationFrame(() => window.scrollTo(0, e.state.scrollY));
  }
  return true;
}

function _destroySessionOverlay() {
  if (!sessionViewHost || !window.Alpine) {
    if (sessionViewHost) sessionViewHost.innerHTML = '';
    return;
  }
  Array.from(sessionViewHost.children).forEach(child => Alpine.destroyTree(child));
  sessionViewHost.innerHTML = '';
}

async function _openSessionOverlay(basePath) {
  if (!sessionViewLayer || !sessionViewHost) return false;
  _sessionOverlayBasePath = basePath || _sessionOverlayBasePath || '/sessions';
  await renderSessionViewFragment(sessionViewHost);
  _showSessionOverlayChrome();
  return true;
}

function _closeSessionOverlay() {
  _hideSessionOverlayChrome();
  _destroySessionOverlay();
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
    s.src = '/static/vendor/html2canvas-1.4.1.min.js';
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
  const iframe = document.querySelector('iframe.design-variant-iframe[data-variant]') ||
    document.getElementById('design-iframe');
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
function _esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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

function _pluginStyleHref(plugin) {
  const rev = plugin && plugin.asset_rev ? `?v=${encodeURIComponent(plugin.asset_rev)}` : '';
  return `/static/plugins/${plugin.id}/page.css${rev}`;
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
      if (!(existing && existing.dataset.assetRev === (p.asset_rev || ''))) {
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

      // Plugins declare an optional page.css (manifest `assets.style`) —
      // served as a static file, but nothing linked it into the page
      // until now. Mirrors the script injection above: one <link> per
      // plugin, keyed by data-plugin-id, refreshed when asset_rev changes.
      const existingLink = document.querySelector(`link[data-plugin-id="${p.id}"]`);
      if (!p.has_style) {
        if (existingLink) existingLink.remove();
      } else if (!existingLink || existingLink.dataset.assetRev !== (p.asset_rev || '')) {
        if (existingLink) existingLink.remove();
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = _pluginStyleHref(p);
        link.dataset.pluginId = p.id;
        link.dataset.assetRev = p.asset_rev || '';
        document.head.appendChild(link);
      }
    }
    if (pending.length) {
      await Promise.all(pending);
    }
    window.dispatchEvent(new CustomEvent('autonomy:plugins-changed', {
      detail: { plugins: window.Autonomy.plugins },
    }));
    return window.Autonomy.plugins;
  } catch (e) {
    window.Autonomy.plugins = [];
    return [];
  }
};

window.Autonomy.matchPlugin = function (path) {
  const list = window.Autonomy.plugins || [];
  for (const p of list) {
    const paths = Array.isArray(p.paths) && p.paths.length ? p.paths : [p.path];
    if (paths.some(pluginPath => path === pluginPath || path.startsWith(pluginPath + '/'))) {
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
    if (p.sidebar === false) continue;
    const a = document.createElement('a');
    a.href = p.path;
    a.className = 'nav-link';
    a.dataset.page = p.id;
    const paths = Array.isArray(p.paths) && p.paths.length ? p.paths : [p.path];
    a.dataset.activeMatch = paths.map(path => String(path || '').replace(/^\//, '')).join(',');
    a.setAttribute('onclick', 'closeSidebar()');
    a.textContent = p.label + ' ';
    const badge = document.createElement('span');
    badge.id = `badge-plugin-${p.id}`;
    badge.dataset.testid = `badge-plugin-${p.id}`;
    badge.className = `nav-badge nav-badge-${p.badge_color || 'gray'}`;
    a.appendChild(badge);
    slot.appendChild(a);
  }
  if (window._sseCache && window._sseCache.nav) {
    _applyNavBadges(window._sseCache.nav);
  }
  if (window._sseCache && window._sseCache.plugin_badges) {
    _applyPluginBadges(window._sseCache.plugin_badges);
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

// Operator-activity heartbeat. Navigation is a REAL operator action — unlike a
// visible tab or a timer, which an abandoned-but-open tab would fake — so it's
// the only honest signal that the operator is actually here. It keeps the 15-min
// idle gate (OperatorActivity, which feeds e.g. the harness-usage strip) from
// marking an active operator idle. Debounced to one POST per window: a single
// write already keeps you non-idle for the full 15 minutes, so writing on every
// navigation would be pointless.
var _ACTIVITY_DEBOUNCE_MS = 60000;
var _lastActivityPing = 0;
function _recordOperatorActivity() {
  var now = Date.now();
  if (now - _lastActivityPing < _ACTIVITY_DEBOUNCE_MS) return;
  _lastActivityPing = now;
  try {
    var f = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
    f('/api/operator/active', { method: 'POST', keepalive: true }).catch(function () {});
  } catch (_e) {}
}

// ── Last-session restore ─────────────────────────────────────
// Persist the canonical path of the most-recently-viewed session. Keyed on
// the full /session/<project>/<name> URL so restore is a no-op lookup.
const _LAST_SESSION_KEY = 'lastSessionPath';
const _LAST_SESSION_TS_KEY = 'lastSessionTs';

function _recordLastSessionView(path) {
  try {
    localStorage.setItem(_LAST_SESSION_KEY, path);
    localStorage.setItem(_LAST_SESSION_TS_KEY, String(Date.now()));
  } catch (_e) { /* private mode / quota — non-fatal */ }
}

// On a cold app start that lands on the home start_url ('/'), drop straight
// back into the most-recently-viewed session IF it is still live — mirrors a
// native app reopening to its last screen. Runs once per page load (only from
// the boot path), so client-side nav to the home page never triggers it: no
// redirect loop, no surprise once you're already using the app.
//
// Non-blocking by design — the home page has already rendered (route() ran
// first); this async liveness check only *then* navigates, so initial paint
// pays zero extra cost. If the session is dead (or the check fails) we leave
// the operator on the home page.
async function _maybeRestoreLastSession() {
  // Home landing only. The PWA start_url is '/', which the server 307s to
  // '/beads' — so a cold boot lands on one of these. Restoring only from the
  // home page means a deep-linked reload (already on a session URL, or any
  // other page) is left untouched.
  const home = window.location.pathname;
  if (home !== '/' && home !== '/beads') return;
  let stored;
  try { stored = localStorage.getItem(_LAST_SESSION_KEY); } catch (_e) { return; }
  if (!stored || !_isSessionPath(stored)) return;
  // Liveness gate: the tmux name is the last path segment.
  const tmux = stored.split('/').pop();
  try {
    const res = await fetch('/api/session/' + encodeURIComponent(tmux));
    if (!res.ok) return;                       // 404 = pruned/unknown session
    const data = await res.json();
    if (!data.is_live) return;                 // dead session — stay home
  } catch (_e) { return; }
  // Bail if the operator navigated away while the check was in flight.
  if (window.location.pathname !== home) return;
  navigateTo(stored);                          // pushState — back returns home
}

// Open the real nav drawer over a covering fragment (the mission screen
// iframe). Only the phone drawer can rise above the frame -- the desktop
// sidebar is static-positioned and z-index cannot lift it -- so the caller
// gets an honest false there and picks its own fallback.
window.Autonomy = window.Autonomy || {};
window.Autonomy.openNav = function () {
  const sb = document.getElementById('sidebar');
  if (!sb || window.innerWidth >= 768) return false;
  document.body.classList.add('nav-above-mission');
  sb.classList.remove('-translate-x-full');
  return true;
};

function navigateTo(path) {
  if (path === window.location.pathname + window.location.search) return;
  _recordOperatorActivity();
  const fromPath = window.location.pathname;
  history.replaceState({ scrollY: window.scrollY }, '');
  history.pushState({}, '', path);
  // Record the most-recently-viewed session here (not only in route()): the
  // mobile overlay branch below returns BEFORE route() runs, so recording in
  // route() alone misses every session opened from the list on mobile — which
  // is the common case on the iOS PWA. window.location.pathname is the clean
  // canonical path (query stripped) now that pushState has applied it.
  if (_isSessionPath(window.location.pathname)) {
    _recordLastSessionView(window.location.pathname);
  }
  if (fromPath === '/sessions' && _sessionOverlayCanHandle(path)) {
    _openSessionOverlay(fromPath);
    return;
  }
  route();
}

function renderMissionScreenFragment() {
  // Mission screens are deliberately standalone documents — one
  // self-contained artifact serves the dashboard AND the credential-less
  // guest relay frame, so they can never become SPA fragments without
  // forking those two renders. Hosting the same document in a same-origin
  // iframe keeps that contract AND keeps the SPA loaded: entering a
  // mission is a history push, leaving is a pop, and the app never
  // tears down (the teardown was the measured white screen + hung
  // back-swipe on the phone; SSE holds the SPA out of bfcache, so a hard
  // exit always meant a cold reboot on return).
  const src = window.location.pathname + window.location.search
    + window.location.hash;
  _replaceFragment(content,
    '<iframe id="mission-screen-frame" title="Mission screen"'
    + ' style="position:fixed;inset:0;width:100%;height:100%;border:0;'
    + 'z-index:50;background:#0c0f14"></iframe>');
  const frame = document.getElementById('mission-screen-frame');
  if (frame) frame.src = src;
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
  const isSessionViewPage = _isSessionPath(path);
  // Remember the most-recently-viewed session so a cold app restart can drop
  // straight back into it (see _maybeRestoreLastSession). Recorded on the
  // canonical /session/<project>/<name> path so restore needs no extra lookup.
  if (isSessionViewPage) _recordLastSessionView(path);
  const handledBySessionOverlay = _sessionOverlayCanHandle(path);
  const isMobileOverlayViewport = _isMobileOverlayViewport();

  // Toggle between #content and persistent #terminal-page
  const termPage = document.getElementById('terminal-page');
  if (isTerminalPage) {
    content.style.display = 'none';
    termPage.style.display = '';
  } else {
    termPage.style.display = 'none';
    content.style.display = '';
  }

  window.Autonomy.resetTopbar();

  // Hide global header on design pages (control strip replaces it)
  const isDesignPage = path.startsWith('/design/');
  const isPresentDeckPage = path.startsWith('/present/')
    || /^\/presentations\/[^/]+/.test(path);
  const globalHeader = document.querySelector('header');
  if (globalHeader) {
    globalHeader.style.display = isDesignPage ? 'none' : '';
  }
  // Remove content padding for full-bleed design and app-owned deck stages.
  content.style.padding = (isDesignPage || isPresentDeckPage) ? '0' : '';
  content.style.overflow = isPresentDeckPage ? 'hidden' : '';

  // Fullscreen page mode: session viewer owns the viewport (hides sidebar + header)
  document.body.classList.toggle(
    'fullscreen-page',
    isSessionViewPage && !handledBySessionOverlay && isMobileOverlayViewport
  );

  // Per-route body class — currently only used by /search to flush its
  // sticky filter strip against the global header (drops the 24px gap
  // caused by main's pt-6 baseline). See bead auto-kvka6 §7.
  document.body.classList.toggle('route-search', path === '/search');
  document.body.classList.toggle('route-present-deck', isPresentDeckPage);

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

  if (handledBySessionOverlay) {
    await _openSessionOverlay(_sessionOverlayBasePath || '/sessions');
    window.dispatchEvent(new CustomEvent('app:navigated', {
      detail: { path: window.location.pathname },
    }));
    return;
  }

  if (!isSessionViewPage && _sessionOverlayBasePath) {
    _sessionOverlayBasePath = null;
    _closeSessionOverlay();
    if (path === '/sessions' && _currentContentPath === '/sessions') {
      pageTitle.textContent = 'Sessions';
      window.dispatchEvent(new CustomEvent('app:navigated', {
        detail: { path: window.location.pathname },
      }));
      return;
    }
  }

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
  } else if (path.match(/^\/session\/[^/]+$/)) {
    resolveBareSessionPath(path);
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
  } else if (path === '/search') {
    renderSearchFragment();
  } else if (path.startsWith('/missions/')) {
    renderMissionScreenFragment();
  } else {
    _replaceFragment(content, '<div class="text-gray-400">Page not found</div>');
  }
  if (!isTerminalPage) {
    _currentContentPath = path;
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

if (globalSearchIcon) {
  globalSearchIcon.addEventListener('click', () => {
    navigateTo('/search');
  });
}

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
  if (_handleSessionOverlayPopstate(e)) return;
  route();
  if (e.state && e.state.scrollY !== undefined) {
    requestAnimationFrame(() => window.scrollTo(0, e.state.scrollY));
  }
});

// ── Init ─────────────────────────────────────────────────────

function _applyNavBadges(data) {
  data = data || {};
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

  _applyPluginBadges(data.plugins);

  // Update pinned beads strip
  if (data.pinned && window.Alpine) {
    Alpine.store('pinned').beads = data.pinned;
  }
  if (_harnessUsageMode !== 'settings' && data.harness_usage) {
    renderHarnessUsage(data.harness_usage);
  }
}

function _applyPluginBadges(plugins) {
  if (!plugins) return;
  if (window._sseCache && window._sseCache.nav) {
    const nav = window._sseCache.nav;
    nav.plugins = Object.assign({}, nav.plugins || {}, plugins);
  }
  Object.keys(plugins).forEach(pluginId => {
    const el = document.getElementById(`badge-plugin-${pluginId}`);
    if (!el) return;
    const badge = plugins[pluginId] && plugins[pluginId].badge;
    el.textContent = badge ? String(badge) : '';
  });
}

// Live dispatch badge via SSE nav topic
connectEvents(['nav', 'dispatch', 'plugin_badges'], {
  dispatch: () => {},  // cache-only — Alpine component handles rendering
  nav: _applyNavBadges,
  plugin_badges: _applyPluginBadges,
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
  // After the home page paints, jump back into the last live session (if any).
  _maybeRestoreLastSession();
})();
