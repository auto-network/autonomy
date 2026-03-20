// Autonomy Dashboard — client-side routing and rendering
// Every view is a function that fetches JSON from the API and renders it.

const content = document.getElementById('content');
const pageTitle = document.getElementById('page-title');
const statsSummary = document.getElementById('stats-summary');
const globalSearch = document.getElementById('global-search');

// ── Screenshot Capture (Design Studio) ───────────────────────
// Persistent MediaStream for tab capture; survives page navigations within SPA.
let _displayStream = null;
let _captureVideo = null;

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
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
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
  content.innerHTML = html;

  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
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
  content.innerHTML = html;

  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
}


// ── Timeline Page (Jinja2 fragment + Alpine) ──────────────────

async function renderTimelineFragment() {
  pageTitle.textContent = 'Timeline';
  let html;
  if (_fragmentCache.has('/pages/timeline')) {
    html = _fragmentCache.get('/pages/timeline');
  } else {
    const res = await fetch('/pages/timeline');
    html = await res.text();
    _fragmentCache.set('/pages/timeline', html);
  }
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
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
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
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
  content.innerHTML = html;

  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
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
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
}

async function renderSearchFragment() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q');
  pageTitle.textContent = q ? `Search: ${q}${params.get('project') ? ' [' + params.get('project') + ']' : ''}` : 'Search';
  let html;
  if (_fragmentCache.has('/pages/search')) {
    html = _fragmentCache.get('/pages/search');
  } else {
    const res = await fetch('/pages/search');
    html = await res.text();
    _fragmentCache.set('/pages/search', html);
  }
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
}

async function renderSourceFragment() {
  const path = window.location.pathname;
  const id = path.split('/source/')[1] || '';
  pageTitle.textContent = `Source: ${id.slice(0, 12)}`;
  let html;
  if (_fragmentCache.has('/pages/source')) {
    html = _fragmentCache.get('/pages/source');
  } else {
    const res = await fetch('/pages/source');
    html = await res.text();
    _fragmentCache.set('/pages/source', html);
  }
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
}

// ── Experiment Page (Jinja2 fragment + Alpine) ───────────────

async function renderExperimentFragment() {
  pageTitle.textContent = 'Experiment';
  // Belt-and-suspenders: clean up any SSE subscription before replacing DOM
  // (Alpine destroy() will also fire, but ordering is not guaranteed).
  if (window._expSeriesCleanup) {
    window._expSeriesCleanup();
    window._expSeriesCleanup = null;
  }
  let html;
  if (_fragmentCache.has('/pages/experiment')) {
    html = _fragmentCache.get('/pages/experiment');
  } else {
    const res = await fetch('/pages/experiment');
    html = await res.text();
    _fragmentCache.set('/pages/experiment', html);
  }
  content.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(content.firstElementChild);
  }
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

// Called by the experiment Alpine component when expanding the Chat With panel.
function _fitChatWithAddon() {
  if (_chatWithFitAddon) _chatWithFitAddon.fit();
}

function connectChatWithTerminal(sessionName) {
  destroyChatWith();

  const container = document.getElementById('chatwith-container');
  if (!container) return;

  // Use Alpine bridge if the experiment Alpine component is active; otherwise
  // fall back to direct DOM manipulation for the legacy (non-Alpine) path.
  const page = window._experimentPage;
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

async function spawnChatWith(expId) {
  const btn = document.getElementById('chatwith-btn');
  const statusEl = document.getElementById('chatwith-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Spawning...'; }
  if (statusEl) { statusEl.textContent = 'spawning...'; statusEl.className = 'text-xs text-yellow-400 ml-2'; }

  const panel = document.getElementById('chatwith-panel');
  if (panel) panel.style.display = '';

  try {
    const res = await fetch('/api/chatwith/spawn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({page_type: 'experiment', context_id: expId}),
    });
    const result = await res.json();
    if (result.error) {
      if (btn) { btn.disabled = false; btn.textContent = 'Chat With'; }
      if (statusEl) { statusEl.textContent = `Error: ${result.error}`; statusEl.className = 'text-xs text-red-400 ml-2'; }
      return;
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Reconnect'; }
    connectChatWithTerminal(result.session_name);
    // Start display capture now that Chat With is active
    initDisplayCapture(expId).catch(() => {});
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Chat With'; }
    if (statusEl) { statusEl.textContent = 'spawn failed'; statusEl.className = 'text-xs text-red-400 ml-2'; }
    console.error('spawnChatWith error:', e);
  }
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

async function killChatWithSession(expId) {
  const sessionName = `chatwith-${expId}`;
  destroyChatWith();
  const panel = document.getElementById('chatwith-panel');
  if (panel) panel.style.display = 'none';
  const btn = document.getElementById('chatwith-btn');
  if (btn) { btn.textContent = 'Chat With'; btn.disabled = false; }
  await fetch(`/api/terminal/${sessionName}/kill`);
}


// ── Terminal ─────────────────────────────────────────────────

let activeTerm = null;
let activeWs = null;
let activeTerminalId = null;
let _terminalFitAddon = null;
let _terminalResizeObserver = null;
let _terminalFragmentInit = false;

function destroyTerminal() {
  if (_terminalResizeObserver) { _terminalResizeObserver.disconnect(); _terminalResizeObserver = null; }
  if (activeWs) { try { activeWs.close(); } catch(e) {} activeWs = null; }
  if (activeTerm) { activeTerm.dispose(); activeTerm = null; }
  _terminalFitAddon = null;
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
  termPage.innerHTML = html;
  if (window.Alpine) {
    Alpine.initTree(termPage.firstElementChild);
  }
  _terminalFragmentInit = true;
}

async function renderTerminal(cmd, attach) {
  // Auto-reconnect to previously active session when navigating back
  if (!cmd && !attach && activeTerminalId) {
    attach = activeTerminalId;
  }
  pageTitle.textContent = attach ? `Attached: ${attach}` : 'Terminal';

  // Ensure Alpine chrome is rendered (idempotent after first call)
  await renderTerminalFragment();

  // If we already have an active connection to the requested session, just re-fit
  if (!cmd && attach && activeTerm && activeWs && activeWs.readyState === WebSocket.OPEN
      && activeTerminalId === attach) {
    if (_terminalFitAddon) _terminalFitAddon.fit();
    activeTerm.focus();
    return;
  }

  // Update active terminal ID
  if (attach) {
    activeTerminalId = attach;
    if (window._terminalPage) window._terminalPage.setActiveId(attach);
  } else if (cmd) {
    activeTerminalId = null;
    if (window._terminalPage) window._terminalPage.setActiveId(null);
  }

  if (!attach && !cmd) return;

  // Need to create or switch session — destroy previous
  destroyTerminal();

  const termContainer = document.getElementById('terminal-container');
  termContainer.innerHTML = '';
  if (window._terminalPage) {
    window._terminalPage.setStatus('connecting...', 'text-xs text-yellow-400');
  }

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
  _terminalFitAddon = fitAddon;
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
    if (window._terminalPage) {
      window._terminalPage.setStatus('connected', 'text-xs text-green-400');
    }
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
        if (window._terminalPage) window._terminalPage.setActiveId(newest.id);
      }
    }
    // Refresh pill bar now that tmux session exists
    if (window._terminalPage) await window._terminalPage.refresh();
    term.focus();
  };

  ws.onmessage = (e) => {
    term.write(e.data);
  };

  ws.onclose = () => {
    if (window._terminalPage) {
      window._terminalPage.setStatus('disconnected', 'text-xs text-red-400');
    }
    term.write('\r\n\x1b[90m--- session ended ---\x1b[0m\r\n');
  };

  ws.onerror = () => {
    if (window._terminalPage) {
      window._terminalPage.setStatus('error', 'text-xs text-red-400');
    }
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

  // Handle resize (guard against 0x0 when terminal page is hidden)
  if (_terminalResizeObserver) _terminalResizeObserver.disconnect();
  _terminalResizeObserver = new ResizeObserver((entries) => {
    const entry = entries[0];
    if (!entry || entry.contentRect.width === 0 || entry.contentRect.height === 0) return;
    if (_terminalFitAddon) _terminalFitAddon.fit();
    const dims = _terminalFitAddon ? _terminalFitAddon.proposeDimensions() : null;
    if (dims && activeWs && activeWs.readyState === WebSocket.OPEN) {
      activeWs.send(`\x1b[8;${dims.rows};${dims.cols}t`);
    }
  });
  _terminalResizeObserver.observe(termContainer);
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

// ── Experiment Gallery ────────────────────────────────────────

/**
 * Request a tab display stream once per session.
 * getDisplayMedia requires a user gesture OR page load in some browsers.
 * Falls back gracefully if unavailable or denied.
 */
async function initDisplayCapture(expId) {
  if (_displayStream) return; // already have a live stream
  if (!navigator.mediaDevices?.getDisplayMedia) {
    console.warn('[screenshot] getDisplayMedia not available (non-HTTPS?)');
    _updateScreenshotStatus(expId, 'Auto-capture unavailable — use Capture button');
    return;
  }
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
    });
    _updateScreenshotStatus(expId, 'Auto-capture active');
    // Stream is now ready — capture after a short delay for rendering
    setTimeout(() => captureTabScreenshot(expId), 1500);
  } catch (e) {
    console.warn('[screenshot] getDisplayMedia denied or failed:', e.message);
    _displayStream = null;
    _captureVideo = null;
    _updateScreenshotStatus(expId, 'Auto-capture denied — use Capture button');
  }
}

function _updateScreenshotStatus(expId, msg) {
  if (window._experimentPage) {
    window._experimentPage.setScreenshotStatus(msg);
  } else {
    const el = document.getElementById('exp-screenshot-status');
    if (el) el.textContent = msg;
  }
}

/** Grab a frame from the active display stream and POST to server. */
async function captureTabScreenshot(expId) {
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
    const res = await fetch(`/api/experiments/${expId}/screenshot`, {
      method: 'POST',
      headers: { 'Content-Type': 'image/png' },
      body: blob,
    });
    if (res.ok) {
      const now = new Date().toLocaleTimeString();
      _updateScreenshotStatus(expId, `Screenshot saved ${now}`);
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
async function _captureViaIframeHtml2Canvas(expId) {
  const iframe = document.querySelector('iframe.exp-variant-iframe[data-variant]');
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
    const res = await fetch(`/api/experiments/${expId}/screenshot`, {
      method: 'POST',
      headers: { 'Content-Type': 'image/png' },
      body: blob,
    });
    if (res.ok) {
      const now = new Date().toLocaleTimeString();
      _updateScreenshotStatus(expId, `Screenshot saved ${now}`);
    }
    return true;
  } catch (e) {
    console.warn('[screenshot] iframe html2canvas failed:', e.message);
    return false;
  }
}

/** Fallback: capture visible page using html2canvas (same-origin, no getDisplayMedia needed). */
async function _captureWithHtml2Canvas(expId) {
  // Try iframe-based capture first (works on mobile where parent can't see into iframes)
  if (await _captureViaIframeHtml2Canvas(expId)) return;
  // Fall back to parent-page capture
  try {
    const h2c = await _ensureHtml2Canvas(document, window);
    const canvas = await h2c(document.getElementById('content'), {
      useCORS: true, allowTaint: true, logging: false,
    });
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob) return;
    const res = await fetch(`/api/experiments/${expId}/screenshot`, {
      method: 'POST',
      headers: { 'Content-Type': 'image/png' },
      body: blob,
    });
    if (res.ok) {
      const now = new Date().toLocaleTimeString();
      _updateScreenshotStatus(expId, `Screenshot (fallback) saved ${now}`);
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
async function manualCaptureScreenshot(expId) {
  _updateScreenshotStatus(expId, 'Capturing...');
  if (_displayStream) {
    await captureTabScreenshot(expId);
    return;
  }
  // Try to acquire stream via user gesture
  try {
    await initDisplayCapture(expId);
    if (_displayStream) {
      await new Promise(r => setTimeout(r, 300)); // let video initialize
      await captureTabScreenshot(expId);
      return;
    }
  } catch (e) { /* fall through */ }
  // Fall back to html2canvas
  await _captureWithHtml2Canvas(expId);
}

async function renderExperiment(expId) {
  // Clean up any previous experiment SSE subscription
  if (window._expSeriesCleanup) {
    window._expSeriesCleanup();
    window._expSeriesCleanup = null;
  }

  pageTitle.textContent = 'Experiment';
  content.innerHTML = '<div class="text-gray-400">Loading experiment...</div>';
  destroyChatWith();
  _chatWithCollapsed = false;

  const exp = await api(`/api/experiments/${expId}/full`);
  if (exp.error) {
    content.innerHTML = `<div class="text-red-400">Experiment not found</div>`;
    return;
  }

  const variants = exp.variants || [];
  const isCompleted = exp.status === 'completed';

  // Session context: use series_id when available so all series iterations
  // share one persistent Chat With session ("chatwith-{series_id}")
  const sessionCtx = exp.series_id || expId;

  // Series navigation
  const siblingIds = exp.sibling_ids || [expId];
  const seriesIdx = siblingIds.indexOf(expId);
  const seriesTotal = siblingIds.length;
  const isInSeries = seriesTotal > 1;
  const prevId = isInSeries && seriesIdx > 0 ? siblingIds[seriesIdx - 1] : null;
  const nextId = isInSeries && seriesIdx < seriesTotal - 1 ? siblingIds[seriesIdx + 1] : null;

  // Track selection state
  const selected = new Map();
  if (isCompleted) {
    variants.forEach(v => { if (v.selected) selected.set(v.id, v.rank); });
  }

  const seriesNav = isInSeries ? `
    <div class="flex items-center gap-3 mb-3 text-sm">
      ${prevId
        ? `<button class="text-indigo-400 hover:text-indigo-300" onclick="navigateTo('/experiments/${_esc(prevId)}')">\u2190 Prev</button>`
        : `<span class="text-gray-600">\u2190 Prev</span>`}
      <span class="text-gray-400">Iteration ${seriesIdx + 1} of ${seriesTotal}</span>
      ${nextId
        ? `<button class="text-indigo-400 hover:text-indigo-300" onclick="navigateTo('/experiments/${_esc(nextId)}')">Next \u2192</button>`
        : `<span class="text-gray-600">Next \u2192</span>`}
    </div>` : '';

  // Populate header action buttons (sticky top nav)
  const headerActions = document.getElementById('header-actions');
  if (headerActions) {
    headerActions.innerHTML = `
      <span id="exp-screenshot-status" class="text-xs text-gray-500"></span>
      <button onclick="manualCaptureScreenshot('${_esc(expId)}')"
              class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-500 hover:text-gray-300 hover:border-gray-500 transition-colors">
        Capture
      </button>
      <button id="chatwith-btn" onclick="spawnChatWith('${_esc(expId)}')"
              class="px-3 py-1 bg-indigo-700 hover:bg-indigo-600 rounded text-sm text-white">
        Chat With
      </button>
    `;
  }

  let html = `
    <div class="max-w-6xl mx-auto">
      <h2 class="text-xl font-bold text-indigo-400 mb-1">${_esc(exp.title)}</h2>
      ${seriesNav}
      ${exp.description ? `<p class="text-gray-400 text-sm mb-4">${_esc(exp.description)}</p>` : ''}
      ${isCompleted ? '<p class="text-green-400 text-sm mb-4 font-semibold">Results submitted</p>' : ''}
      <div id="exp-variants">`;

  variants.forEach(v => {
    const isSelected = isCompleted && v.selected;
    html += `
        <div class="exp-variant" data-variant-id="${_esc(v.id)}">
          <div class="exp-variant-header">
            <span class="exp-variant-label">${_esc(v.id)}</span>
            <div class="flex items-center gap-2">
              <span class="exp-rank-wrap" style="display:none;">
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
          <iframe class="exp-variant-iframe" data-variant="${_esc(v.id)}" style="min-height:300px;"></iframe>
        </div>`;
  });

  html += `</div>`;

  if (!isCompleted) {
    html += `
      <div class="exp-submit-bar" id="exp-submit-bar">
        <span class="text-sm text-gray-400" id="exp-selection-hint">Select variants to rank them</span>
        <button class="exp-submit-btn" id="exp-submit-btn" disabled onclick="submitExperiment('${expId}')">Submit Rankings</button>
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
  content.innerHTML = html;

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
        initDisplayCapture(expId).catch(() => {});
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
          await captureTabScreenshot(expId);
        } else {
          await _captureViaIframeHtml2Canvas(expId);
        }
      }, 1500);
    }
  });

  // Subscribe to series SSE topic so gallery auto-updates when a new variant is posted
  const seriesId = exp.series_id;
  if (seriesId) {
    const seriesTopic = `experiments:${seriesId}`;

    function _onNewSeriesExperiment(data) {
      // Ignore replays of the current experiment
      const currentId = window.location.pathname.split('/experiments/')[1];
      if (!currentId || data.experiment_id === currentId) return;
      // Only act if still on an experiment page (user may have navigated away)
      if (!window.location.pathname.startsWith('/experiments/')) return;
      navigateTo(`/experiments/${data.experiment_id}`);
    }

    registerHandler(seriesTopic, _onNewSeriesExperiment);
    window._expSeriesCleanup = () => unregisterHandler(seriesTopic, _onNewSeriesExperiment);
  }

  // Display capture is initiated when Chat With spawns, not on page load.
  // If a stream is already active (from a previous Chat With), auto-capture.
  if (!isCompleted && _displayStream) {
    setTimeout(() => captureTabScreenshot(expId), 1500);
  }

  if (isCompleted) {
    // Show rank badges on completed variants
    variants.forEach(v => {
      if (v.selected && v.rank != null) {
        const rankWrap = content.querySelector(`[data-variant="${v.id}"]`)?.closest('.exp-variant')?.querySelector('.exp-rank-wrap');
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
    content.querySelectorAll('.exp-rank-wrap').forEach(el => {
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

async function submitExperiment(expId) {
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

  const res = await fetch(`/api/experiments/${expId}/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selections }),
  });
  const data = await res.json();

  if (data.ok) {
    btn.textContent = 'Submitted';
    // Dismiss sidebar indicator for this experiment
    const indicator = document.querySelector(`[data-exp-id="${expId}"]`);
    if (indicator) indicator.remove();
    // Refresh page to show completed state
    setTimeout(() => renderExperiment(expId), 500);
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

// ── Sidebar Experiment Indicator ─────────────────────────────

async function dismissExperiment(expId, seriesKey, triggerEl) {
  const sessionName = `chatwith-${seriesKey}`;
  const toastEl = triggerEl.closest('[data-exp-id]');
  const hasChatWith = toastEl && toastEl.dataset.hasChatwith === 'true';
  const confirmMsg = hasChatWith ? 'End experiment and Chat With session?' : 'End?';

  const confirmed = await new Promise(resolve => {
    const confirm = document.createElement('span');
    confirm.className = 'exp-dismiss-confirm';
    confirm.innerHTML = `${confirmMsg} <button class="exp-dismiss-yes">Yes</button><button class="exp-dismiss-no">No</button>`;
    triggerEl.replaceWith(confirm);
    confirm.querySelector('.exp-dismiss-yes').onclick = (e) => { e.stopPropagation(); resolve(true); };
    confirm.querySelector('.exp-dismiss-no').onclick = (e) => { e.stopPropagation(); confirm.replaceWith(triggerEl); resolve(false); };
  });
  if (!confirmed) return;
  try {
    await fetch(`/api/experiments/${expId}/dismiss`, { method: 'POST' });
  } catch (e) {
    console.warn('[dismiss] Failed to dismiss experiment:', e);
  }
  if (hasChatWith) {
    try {
      await fetch(`/api/terminal/${sessionName}/kill`);
    } catch (e) {
      console.warn('[dismiss] Failed to kill Chat With session:', e);
    }
  }
  const el = document.querySelector(`[data-exp-id="${seriesKey}"]`);
  if (el) el.remove();
}

async function dismissOrphanSession(sessionName, orphanKey, triggerEl) {
  const confirmed = await new Promise(resolve => {
    const confirm = document.createElement('span');
    confirm.className = 'exp-dismiss-confirm';
    confirm.innerHTML = 'Kill session? <button class="exp-dismiss-yes">Yes</button><button class="exp-dismiss-no">No</button>';
    triggerEl.replaceWith(confirm);
    confirm.querySelector('.exp-dismiss-yes').onclick = (e) => { e.stopPropagation(); resolve(true); };
    confirm.querySelector('.exp-dismiss-no').onclick = (e) => { e.stopPropagation(); confirm.replaceWith(triggerEl); resolve(false); };
  });
  if (!confirmed) return;
  try {
    await fetch(`/api/terminal/${sessionName}/kill`);
  } catch (e) {
    console.warn('[dismiss] Failed to kill orphan Chat With session:', e);
  }
  const el = document.querySelector(`[data-exp-id="${orphanKey}"]`);
  if (el) el.remove();
}

async function checkPendingExperiments() {
  const container = document.getElementById('sidebar-experiments');
  if (!container) return;
  try {
    const [pending, chatData] = await Promise.all([
      api('/api/experiments/pending'),
      api('/api/chatwith/sessions').catch(() => ({ sessions: [] })),
    ]);
    // Build a mutable set of active chatwith session names; we'll delete matched ones
    // to find orphaned sessions afterward.
    const activeSessions = new Set((chatData && chatData.sessions) || []);
    const keys = new Set();

    if (Array.isArray(pending)) {
      pending.forEach(exp => {
        // Use series_id as the stable key so the toast represents the whole series
        const seriesKey = exp.series_id || exp.id;
        keys.add(seriesKey);
        const sessionName = `chatwith-${seriesKey}`;
        const hasChatWith = activeSessions.has(sessionName);
        // Consume session so it isn't treated as orphaned below
        activeSessions.delete(sessionName);

        const iterLabel = exp.iteration_count > 1
          ? `${_esc(exp.title)} <span class="text-gray-500 text-xs">(${exp.iteration_count} iterations)</span>`
          : _esc(exp.title);
        const existing = container.querySelector(`[data-exp-id="${seriesKey}"]`);
        if (existing) {
          // Update link target, label, and Chat With indicator
          existing.dataset.hasChatwith = hasChatWith ? 'true' : 'false';
          existing.href = `/experiments/${exp.id}`;
          existing.onclick = (e) => { e.preventDefault(); navigateTo(`/experiments/${exp.id}`); };
          const textEl = existing.querySelector('.sidebar-exp-text');
          if (textEl) textEl.innerHTML = iterLabel;
          // Sync pulsing dot
          let dot = existing.querySelector('.sidebar-exp-chat-dot');
          if (hasChatWith && !dot) {
            dot = document.createElement('span');
            dot.className = 'sidebar-exp-chat-dot';
            const icon = existing.querySelector('.sidebar-exp-icon');
            existing.insertBefore(dot, icon ? icon.nextSibling : textEl);
          } else if (!hasChatWith && dot) {
            dot.remove();
          }
          return;
        }
        const link = document.createElement('a');
        link.className = 'sidebar-exp';
        link.dataset.expId = seriesKey;
        link.dataset.hasChatwith = hasChatWith ? 'true' : 'false';
        // Navigate to latest iteration (exp.id is already the latest from the API)
        link.href = `/experiments/${exp.id}`;
        link.onclick = (e) => { e.preventDefault(); navigateTo(`/experiments/${exp.id}`); };
        const chatDot = hasChatWith ? '<span class="sidebar-exp-chat-dot"></span>' : '';
        link.innerHTML = `<span class="sidebar-exp-icon">\uD83E\uDDEA</span>${chatDot}<span class="sidebar-exp-text">${iterLabel}</span>`;
        const btn = document.createElement('button');
        btn.className = 'sidebar-exp-dismiss';
        btn.title = 'Dismiss';
        btn.textContent = '\u00d7';
        btn.onclick = (e) => { e.preventDefault(); e.stopPropagation(); dismissExperiment(exp.id, seriesKey, btn); };
        link.appendChild(btn);
        container.appendChild(link);
      });
    }

    // Orphaned Chat With sessions: active tmux sessions with no matching pending experiment
    for (const sessionName of activeSessions) {
      if (!sessionName.startsWith('chatwith-')) continue;
      const orphanKey = `orphan-${sessionName}`;
      keys.add(orphanKey);
      if (container.querySelector(`[data-exp-id="${orphanKey}"]`)) continue;
      const link = document.createElement('a');
      link.className = 'sidebar-exp';
      link.dataset.expId = orphanKey;
      link.dataset.hasChatwith = 'true';
      link.href = '#';
      link.onclick = (e) => e.preventDefault();
      link.innerHTML = `<span class="sidebar-exp-icon">\uD83D\uDCAC</span><span class="sidebar-exp-chat-dot"></span><span class="sidebar-exp-text">${_esc(sessionName)}</span>`;
      const btn = document.createElement('button');
      btn.className = 'sidebar-exp-dismiss';
      btn.title = 'Kill session';
      btn.textContent = '\u00d7';
      btn.onclick = (e) => { e.preventDefault(); e.stopPropagation(); dismissOrphanSession(sessionName, orphanKey, btn); };
      link.appendChild(btn);
      container.appendChild(link);
    }

    // Remove indicators for series that are no longer pending (server is source of truth)
    container.querySelectorAll('[data-exp-id]').forEach(el => {
      if (!keys.has(el.dataset.expId)) el.remove();
    });
  } catch(e) {}
}

// ── Router ───────────────────────────────────────────────────

function navigateTo(path) {
  history.pushState({}, '', path);
  route();
}

function route() {
  _checkVersion();
  const path = window.location.pathname;
  const isTerminalPage = path === '/terminal' || path.startsWith('/terminal/');

  // Toggle between #content and persistent #terminal-page
  const termPage = document.getElementById('terminal-page');
  if (isTerminalPage) {
    content.style.display = 'none';
    termPage.style.display = '';
  } else {
    termPage.style.display = 'none';
    content.style.display = '';
  }

  // Clear header action buttons from previous page
  const headerActions = document.getElementById('header-actions');
  if (headerActions) headerActions.innerHTML = '';

  // Clear any auto-refresh intervals from previous page (managed by Alpine lifecycle)

  // Update active nav
  document.querySelectorAll('.nav-link').forEach(el => {
    el.classList.toggle('active', path.startsWith('/' + el.dataset.page));
  });

  // Update global search placeholder based on page
  globalSearch.placeholder = (path === '/' || path === '/beads') ? 'Search beads...' : 'Search graph...';

  if (path === '/' || path === '/beads') {
    renderBeadsFragment();
  } else if (path.startsWith('/dispatch/trace/')) {
    renderTraceFragment();
  } else if (path === '/dispatch' || path === '/dispatch/alpine' || path === '/dispatch/lit') {
    renderDispatchFragment();
  } else if (path.startsWith('/bead/')) {
    renderBeadDetailFragment(path.split('/bead/')[1]);
  } else if (path === '/timeline') {
    renderTimelineFragment();
  } else if (path === '/sessions') {
    renderSessionsFragment();
  } else if (path.match(/^\/session\/[^/]+\/.+$/)) {
    renderSessionViewFragment();
  } else if (path === '/search') {
    renderSearchFragment();
  } else if (path.startsWith('/source/')) {
    renderSourceFragment();
  } else if (isTerminalPage) {
    const sessionId = path.startsWith('/terminal/') ? path.split('/terminal/')[1] : null;
    renderTerminal(null, sessionId);
  } else if (path.startsWith('/experiments/')) {
    renderExperimentFragment();
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
        // On beads page: Alpine component reacts to input events — no extra action needed
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

    const beadsEl = document.getElementById('badge-beads');
    if (beadsEl && data.open_beads != null) beadsEl.textContent = data.open_beads || '';

    const sessionsEl = document.getElementById('badge-sessions');
    if (sessionsEl) sessionsEl.textContent = data.active_sessions || '';

    const timelineEl = document.getElementById('badge-timeline');
    if (timelineEl) timelineEl.textContent = data.today_done || '';

    const terminalEl = document.getElementById('badge-terminal');
    if (terminalEl) terminalEl.textContent = data.terminal_count || '';
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

// connectEvents() is defined in static/js/events.js (loaded before this file).

// Check for pending experiments
checkPendingExperiments();
setInterval(checkPendingExperiments, 10000);

// Initial route
route();
