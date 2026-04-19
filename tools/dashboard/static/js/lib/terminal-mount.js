/**
 * mountTerminal — attach an xterm.js instance to a DOM container and bridge
 * it to the dashboard /ws/terminal endpoint.
 *
 * Extracted from app.js renderTerminal() so the same bridge can be reused
 * by the session viewer's terminal-toggle overlay without duplicating the
 * xterm config, clipboard, paste, and resize wiring.
 *
 * Usage:
 *   const t = window.mountTerminal(container, tmuxName);
 *   // ... later ...
 *   t.fit();
 *   t.dispose();
 *
 * Options:
 *   onStatus(state, message) — fires on 'connecting' | 'connected' |
 *     'disconnected' | 'error' transitions. Callers that want status pills
 *     pass a handler; others ignore.
 *   onOpen() — fires after the WebSocket opens and initial dimensions are
 *     sent (the /terminal page uses it to refresh the pill bar).
 */
(function () {
  window.mountTerminal = function (container, tmuxName, options) {
    options = options || {};
    var onStatus = options.onStatus || function () {};
    var onOpen = options.onOpen || function () {};

    container.innerHTML = '';
    onStatus('connecting', 'connecting...');

    var term = new Terminal({
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

    var fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    if (typeof ClipboardAddon !== 'undefined') {
      term.loadAddon(new ClipboardAddon.ClipboardAddon());
    }
    term.open(container);
    try { fitAddon.fit(); } catch (e) {}

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var wsUrl = proto + '//' + location.host + '/ws/terminal?attach=' + encodeURIComponent(tmuxName);
    var ws = new WebSocket(wsUrl);

    ws.onopen = function () {
      onStatus('connected', 'connected');
      var dims = fitAddon.proposeDimensions();
      if (dims) ws.send('\x1b[8;' + dims.rows + ';' + dims.cols + 't');
      try { onOpen(); } catch (e) {}
      try { term.focus(); } catch (e) {}
    };

    ws.onmessage = function (e) {
      term.write(e.data);
    };

    ws.onclose = function () {
      onStatus('disconnected', 'disconnected');
      try {
        term.write('\r\n\x1b[90m--- session ended ---\x1b[0m\r\n');
      } catch (e) {}
    };

    ws.onerror = function () {
      onStatus('error', 'error');
    };

    term.onData(function (data) {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    });

    // ── Clipboard: selection → clipboard (Shift-select bypass of tmux mouse) ──
    function copyToClipboard(text) {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).catch(function () {});
      } else {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
    }
    term.onSelectionChange(function () {
      var sel = term.getSelection();
      if (sel) copyToClipboard(sel);
    });

    // ── Paste handling (bracket paste + Ctrl+Shift+V) ──
    function bracketPaste(text) {
      if (text.indexOf('\n') >= 0 || text.indexOf('\r') >= 0) {
        return '\x1b[200~' + text + '\x1b[201~';
      }
      return text;
    }
    function pasteFromClipboard() {
      if (!navigator.clipboard) return;
      navigator.clipboard.readText().then(function (text) {
        if (text && ws.readyState === WebSocket.OPEN) {
          ws.send(bracketPaste(text));
        }
      }).catch(function (err) {
        console.warn('Clipboard read failed:', err && err.message);
      });
    }
    term.onPaste(function (text) {
      if (text && ws.readyState === WebSocket.OPEN) {
        ws.send(bracketPaste(text));
      }
    });
    term.attachCustomKeyEventHandler(function (e) {
      if (e.type === 'keydown' && e.ctrlKey && e.shiftKey && e.key === 'V') {
        e.preventDefault();
        pasteFromClipboard();
        return false;
      }
      return true;
    });

    // ── Resize: observe the container and forward tmux resize signals ──
    var resizeObs = null;
    if (typeof ResizeObserver !== 'undefined') {
      resizeObs = new ResizeObserver(function (entries) {
        var entry = entries[0];
        if (!entry || entry.contentRect.width === 0 || entry.contentRect.height === 0) return;
        try { fitAddon.fit(); } catch (e) {}
        var dims = fitAddon.proposeDimensions();
        if (dims && ws.readyState === WebSocket.OPEN) {
          ws.send('\x1b[8;' + dims.rows + ';' + dims.cols + 't');
        }
      });
      resizeObs.observe(container);
    }

    return {
      term: term,
      ws: ws,
      fitAddon: fitAddon,
      dispose: function () {
        try { if (resizeObs) resizeObs.disconnect(); } catch (e) {}
        try { ws.close(); } catch (e) {}
        try { term.dispose(); } catch (e) {}
      },
      fit: function () {
        try { fitAddon.fit(); } catch (e) {}
        var dims = fitAddon.proposeDimensions();
        if (dims && ws.readyState === WebSocket.OPEN) {
          ws.send('\x1b[8;' + dims.rows + ';' + dims.cols + 't');
        }
      },
    };
  };
})();
