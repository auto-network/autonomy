// Terminal page Alpine component.
// Manages the chrome UI: session pill bar, launch buttons, status indicator.
// xterm.js terminal canvas is managed imperatively by renderTerminal() in app.js.
//
// Exposes window._terminalPage so renderTerminal() can push reactive state
// updates (status, activeId, sessions) without depending on Alpine internals.

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('terminalPage', () => ({
      sessions: [],
      status: 'ready',
      statusClass: 'text-xs text-gray-500',
      activeId: null,
      _clickTimer: null,

      init() {
        window._terminalPage = this;
        this.activeId = activeTerminalId;
        this.refresh();
      },

      destroy() {
        if (window._terminalPage === this) window._terminalPage = null;
        clearTimeout(this._clickTimer);
      },

      async refresh() {
        try {
          const data = await fetch('/api/terminals').then(r => r.json());
          this.sessions = (Array.isArray(data) ? data : []).map(t => {
            const cmd = (t.cmd || '').toLowerCase();
            const isClaude = cmd.includes('claude') || cmd.includes('autonomy-agent-claude');
            const isContainer = t.env === 'container' || cmd.includes('docker') || cmd.includes('autonomy-agent');
            return {
              ...t,
              _icon: isClaude ? '🤖' : '⬛',
              _label: isClaude ? 'claude' : 'bash',
              _isContainer: isContainer,
              _envLabel: isContainer ? 'container' : 'host',
            };
          });
        } catch (e) {
          console.warn('[terminalPage] refresh error', e);
        }
      },

      setStatus(text, cls) {
        this.status = text;
        this.statusClass = cls;
      },

      setActiveId(id) {
        this.activeId = id;
        activeTerminalId = id;
      },

      launchClaude() {
        renderTerminal('claude --dangerously-skip-permissions');
      },

      launchClaudeContainer() {
        renderTerminal('autonomy-agent-claude');
      },

      launchBash() {
        renderTerminal('/bin/bash');
      },

      launchBashContainer() {
        renderTerminal('autonomy-agent-bash');
      },

      pillClick(id) {
        clearTimeout(this._clickTimer);
        this._clickTimer = setTimeout(() => {
          this._clickTimer = null;
          if (id === activeTerminalId && activeWs?.readyState === WebSocket.OPEN) return;
          renderTerminal(null, id);
        }, 250);
      },

      startRename(event, id) {
        clearTimeout(this._clickTimer);
        this._clickTimer = null;
        const nameSpan = event.target.closest('.pill-name');
        if (!nameSpan) return;
        const currentName = nameSpan.textContent;
        const input = document.createElement('input');
        input.type = 'text';
        input.value = currentName;
        input.className = 'bg-gray-800 text-white text-sm px-1 w-24 rounded outline-none border border-indigo-500';
        input.style.minWidth = '3rem';
        input.addEventListener('click', e => e.stopPropagation());
        const finish = async (save) => {
          if (save) {
            const newName = input.value.trim();
            if (newName && newName !== currentName) {
              await fetch(`/api/terminal/${id}/rename`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName }),
              });
            }
          }
          this.refresh();
        };
        input.addEventListener('blur', () => finish(true));
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
          if (e.key === 'Escape') { e.preventDefault(); finish(false); }
        });
        nameSpan.replaceWith(input);
        setTimeout(() => { input.focus(); input.select(); }, 0);
      },

      async kill(id) {
        await fetch(`/api/terminal/${id}/kill`);
        if (activeTerminalId === id) {
          this.setActiveId(null);
          destroyTerminal();
          const tc = document.getElementById('terminal-container');
          if (tc) tc.innerHTML = '';
        }
        await this.refresh();
      },
    }));
  });
})();
