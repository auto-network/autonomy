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
/**
 * ensureTerminalLibs — fetch the terminal emulator the first time one opens.
 *
 * The emulator, its two addons and its stylesheet used to sit in the shell's
 * head, so every page in the dashboard blocked its first paint on them. A
 * mission screen, which has no terminal anywhere on it, waited for a terminal
 * emulator before it could draw, and the emulator is the largest asset the
 * dashboard serves.
 *
 * The shell is one document for every route with a client-side router, so
 * there is no per-page template to move them into. Fetching them on the first
 * mount covers both a cold load and a move between pages that never reloads
 * the shell.
 */
(function () {
  var VENDOR = [
    "/static/vendor/xterm-5.5.0.min.js",
    "/static/vendor/addon-fit-0.11.0.min.js",
    "/static/vendor/addon-clipboard-0.2.0.min.js",
  ];
  var STYLE = "/static/vendor/xterm-5.5.0.min.css";
  var pending = null;

  function version() {
    // The build the page was served from, so a deploy invalidates these too
    // rather than serving a stale emulator. Read from the value the shell
    // publishes; anything fetched after render needs the same one.
    var meta = document.querySelector('meta[name="autonomy-static-version"]');
    var v = meta && meta.getAttribute("content");
    return v ? "?v=" + encodeURIComponent(v) : "";
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var el = document.createElement("script");
      el.src = src;
      el.onload = resolve;
      el.onerror = function () { reject(new Error("failed to load " + src)); };
      document.head.appendChild(el);
    });
  }

  window.ensureTerminalLibs = function () {
    if (window.Terminal && window.FitAddon) return Promise.resolve();
    if (pending) return pending;          // one fetch however many callers
    var v = version();
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = STYLE + v;
    document.head.appendChild(link);
    // Sequential: each addon registers against the emulator's global.
    pending = VENDOR.reduce(function (chain, src) {
      return chain.then(function () { return loadScript(src + v); });
    }, Promise.resolve()).catch(function (err) {
      pending = null;                     // a failed load may be retried
      throw err;
    });
    return pending;
  };
})();

(function () {
  function installTouchScrollBridge(container, term) {
    var lastTouchY = null;
    var pixelRemainder = 0;

    function resetTouch() {
      lastTouchY = null;
      pixelRemainder = 0;
    }

    function rowHeight() {
      var screen = term.element && term.element.querySelector('.xterm-screen');
      var rect = screen && screen.getBoundingClientRect();
      if (rect && rect.height > 0 && term.rows > 0) {
        return rect.height / term.rows;
      }
      return 17; // xterm's approximate row height at the configured 14px font.
    }

    function dispatchWheel(touch, direction, pixels) {
      var init = {
        bubbles: true,
        cancelable: true,
        deltaMode: 0, // WheelEvent.DOM_DELTA_PIXEL
        deltaY: direction * pixels,
        clientX: touch.clientX,
        clientY: touch.clientY,
      };
      var wheel;
      try {
        wheel = new WheelEvent('wheel', init);
      } catch (e) {
        // Older WebKit builds do not expose the WheelEvent constructor.
        wheel = document.createEvent('Event');
        wheel.initEvent('wheel', true, true);
        Object.keys(init).forEach(function (key) {
          try { Object.defineProperty(wheel, key, { value: init[key] }); } catch (err) {}
        });
      }
      term.element.dispatchEvent(wheel);
    }

    function onTouchStart(e) {
      if (e.touches.length !== 1) {
        resetTouch();
        return;
      }
      lastTouchY = e.touches[0].clientY;
      pixelRemainder = 0;
    }

    function onTouchMove(e) {
      if (e.touches.length !== 1 || lastTouchY === null || !term.element) return;

      var touch = e.touches[0];
      pixelRemainder += lastTouchY - touch.clientY;
      lastTouchY = touch.clientY;

      // Own the single-finger vertical gesture before xterm sees it. xterm
      // suppresses its built-in touch scrolling while a TUI has enabled mouse
      // reporting, even though wheel events still work in that mode.
      if (e.cancelable) e.preventDefault();
      e.stopPropagation();

      var pixels = rowHeight();
      var lines = pixelRemainder > 0
        ? Math.floor(pixelRemainder / pixels)
        : Math.ceil(pixelRemainder / pixels);
      if (lines === 0) return;

      pixelRemainder -= lines * pixels;
      var direction = lines > 0 ? 1 : -1;
      for (var i = 0; i < Math.abs(lines); i++) {
        // Reuse xterm's wheel path: it scrolls xterm scrollback normally and
        // forwards mouse-wheel reports to interactive TUIs such as Codex.
        dispatchWheel(touch, direction, pixels);
      }
    }

    var startOptions = { capture: true, passive: true };
    var moveOptions = { capture: true, passive: false };
    container.addEventListener('touchstart', onTouchStart, startOptions);
    container.addEventListener('touchmove', onTouchMove, moveOptions);
    container.addEventListener('touchend', resetTouch, startOptions);
    container.addEventListener('touchcancel', resetTouch, startOptions);

    return function () {
      container.removeEventListener('touchstart', onTouchStart, startOptions);
      container.removeEventListener('touchmove', onTouchMove, moveOptions);
      container.removeEventListener('touchend', resetTouch, startOptions);
      container.removeEventListener('touchcancel', resetTouch, startOptions);
    };
  }

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
    var removeTouchScrollBridge = installTouchScrollBridge(container, term);

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
    // term.onPaste is not part of xterm's public API on this version; the
    // Ctrl+Shift+V keybind + pasteFromClipboard() above handles paste via
    // the browser clipboard path instead.
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
        try { removeTouchScrollBridge(); } catch (e) {}
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
