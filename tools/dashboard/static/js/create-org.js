/* Create-organization screen (bead auto-yn5yn).
 *
 * The dedicated create-org surface per the approved Design Studio design
 * fe06a4a3 (rev 2bed959b) — NOT the retired C1 identity ceremony. Two
 * entries: the Get-started Organizations step, and any chrome caller via
 * window.AutonomyCreateOrg.open(). Markup mirrors the design byte-for-byte
 * where it can: same Tailwind utilities, same copy, same data-testids.
 *
 * The mark's color derives ONCE when a name first appears and is then
 * pinned; a manual swatch pick or icon upload always wins and is never
 * re-derived (operator ruling, design rev 5).
 */
(function () {
  'use strict';

  var ORG_PALETTE = ['#6C63FF', '#0EA5E9', '#10B981', '#F59E0B', '#EC4899', '#8B5CF6'];

  // ── pure derivations (unit-tested in Node) ─────────────────────────

  function deriveOrgSlug(name) {
    return (name || '').toLowerCase().trim()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  }

  function deriveOrgInitial(name) {
    var n = (name || '').trim();
    return n ? n.charAt(0).toUpperCase() : '';
  }

  function deriveOrgColor(name) {
    var h = 0, s = (name || '');
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return ORG_PALETTE[h % ORG_PALETTE.length];
  }

  // ── state ──────────────────────────────────────────────────────────

  var S = null;   // {entry, phase, name, color, icon, pickerOpen, busy, error}

  function _esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function _fetchJson(url, opts) {
    var resp = await fetch(url, opts);
    var body = await resp.json().catch(function () { return {}; });
    if (!resp.ok) {
      var err = new Error(body.error || ('request failed: ' + url));
      err.status = resp.status;
      throw err;
    }
    return body;
  }

  // ── markup (design fe06a4a3/2bed959b, verbatim classes + copy) ─────

  var _ICON_PENCIL =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ' +
    'class="w-4 h-4 absolute inset-0 m-auto opacity-0 group-hover:opacity-100 transition-opacity drop-shadow">' +
    '<path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

  var _ICON_UPLOAD =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4 text-indigo-400" aria-hidden="true">' +
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>';

  var _ICON_GEAR =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-5 h-5 text-indigo-400">' +
    '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';

  var _ICON_GRID =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-5 h-5 text-indigo-400">' +
    '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M17.5 14v7M14 17.5h7"/></svg>';

  var _ICON_INVITE =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-5 h-5 text-indigo-400">' +
    '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg>';

  function _bulletsHtml() {
    return '<ul class="mt-5 space-y-2.5 text-sm text-gray-300">' +
      '<li class="flex gap-2.5"><span class="text-indigo-400">&#9679;</span>' +
      '<span>Create shared workspaces with the development environment, tools, and skills your organization uses.</span></li>' +
      '<li class="flex gap-2.5"><span class="text-indigo-400">&#9679;</span>' +
      '<span>Build an organizational knowledge graph from your working history, project documentation, and best practices.</span></li>' +
      '<li class="flex gap-2.5"><span class="text-indigo-400">&#9679;</span>' +
      '<span>Starting alone is normal &mdash; invite people to join when you&rsquo;re ready.</span></li></ul>';
  }

  function _swatchesHtml() {
    return ORG_PALETTE.map(function (c) {
      return '<button type="button" data-color="' + c + '" data-testid="create-org-swatch" ' +
        'class="w-8 h-8 rounded-lg focus:outline-none focus:ring-2 focus:ring-white/70' +
        (c === S.color ? ' ring-2 ring-white' : '') + '" ' +
        'style="background:' + c + '" aria-label="Use color ' + c + '"></button>';
    }).join('');
  }

  function _pickerHtml() {
    return '<div id="create-org-mark-picker" data-testid="create-org-mark-picker" ' +
      'class="absolute left-0 top-full mt-2 z-20 bg-gray-900 border border-gray-700 rounded-xl shadow-xl shadow-black/50 p-3 w-64">' +
      '<div class="text-xs text-gray-400 mb-2">Color</div>' +
      '<div class="flex gap-2" id="create-org-swatches">' + _swatchesHtml() + '</div>' +
      '<div class="border-t border-gray-800 mt-3 pt-3">' +
      '<label class="flex items-center gap-2.5 text-sm text-gray-300 hover:text-white cursor-pointer" data-testid="create-org-icon-upload">' +
      _ICON_UPLOAD + '<span>Upload an icon&hellip;</span>' +
      '<input type="file" accept="image/*" class="hidden" id="create-org-icon-input"></label>' +
      (S.icon ? '<button type="button" id="create-org-icon-remove" data-testid="create-org-icon-remove" ' +
        'class="mt-2 text-xs text-gray-500 hover:text-gray-300">Remove icon</button>' : '') +
      '</div></div>';
  }

  function _markHtml() {
    var initial = deriveOrgInitial(S.name);
    if (!initial) return '';
    var inner = S.icon
      ? '<img src="' + _esc(S.icon) + '" class="w-full h-full object-cover" alt="">'
      : '<span class="group-hover:opacity-25 transition-opacity">' + _esc(initial) + '</span>';
    return '<button type="button" id="create-org-mark" data-testid="create-org-mark" ' +
      'class="w-11 h-11 rounded-lg flex items-center justify-center font-bold text-base flex-shrink-0 ' +
      'text-white relative group overflow-hidden focus:outline-none focus:ring-2 focus:ring-indigo-500" ' +
      (S.icon ? '' : 'style="background:' + S.color + '" ') +
      'title="Choose a color or icon" aria-label="Choose a color or upload an icon" ' +
      'aria-expanded="' + (S.pickerOpen ? 'true' : 'false') + '">' +
      inner + _ICON_PENCIL + '</button>';
  }

  function _formHtml() {
    return '<h1 class="text-2xl md:text-xl font-semibold" data-testid="create-org-title">Create a new organization</h1>' +
      '<p class="text-gray-400 text-sm mt-2 leading-relaxed">An organization is how you group, arrange, and share ' +
      'your work. From an organization of one (your private notes, a hobby) to an organization of thousands.</p>' +
      _bulletsHtml() +
      '<label class="block mt-7 text-sm text-gray-300" for="create-org-name">Organization name</label>' +
      '<div class="mt-2 flex items-center gap-3 relative" id="create-org-namerow">' +
      '<span id="create-org-markslot" class="contents">' + _markHtml() + '</span>' +
      '<input id="create-org-name" data-testid="create-org-name" type="text" autocomplete="off" ' +
      'class="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3.5 py-2.5 text-base ' +
      'focus:outline-none focus:border-indigo-500" value="' + _esc(S.name) + '">' +
      '<span id="create-org-pickerslot" class="contents">' + (S.pickerOpen ? _pickerHtml() : '') + '</span>' +
      '</div>' +
      '<p id="create-org-error" data-testid="create-org-error" class="mt-3 text-sm text-rose-400' +
      (S.error ? '' : ' hidden') + '">' + _esc(S.error || '') + '</p>' +
      '<details class="mt-6 group/td"><summary data-testid="create-org-techdetail" ' +
      'class="text-xs text-gray-500 cursor-pointer select-none hover:text-gray-400 list-none [&::-webkit-details-marker]:hidden">' +
      '<span class="group-open/td:hidden">&#9656;</span><span class="hidden group-open/td:inline">&#9662;</span> Technical detail</summary>' +
      '<p class="mt-2 text-xs text-gray-500 leading-relaxed">The organization is created with its own signing root, ' +
      'made on this device and held by you. Anything you do in it is signed by you, carrying the organization&rsquo;s ' +
      'authority. Nothing leaves this device until you invite someone or publish.</p></details>' +
      '<div class="mt-8 flex flex-col-reverse md:flex-row md:justify-end gap-3">' +
      '<button type="button" id="create-org-dismiss" data-testid="create-org-dismiss" ' +
      'class="px-4 py-2.5 rounded-lg text-sm text-gray-400 hover:text-gray-200 border border-gray-700">' +
      (S.entry === 'onboarding' ? 'Later' : 'Not now') + '</button>' +
      '<button type="button" id="create-org-submit" data-testid="create-org-submit" ' +
      'class="px-5 py-2.5 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white ' +
      'disabled:opacity-50 disabled:cursor-not-allowed"' + (S.busy || !deriveOrgSlug(S.name) ? ' disabled' : '') + '>' +
      (S.busy ? 'Creating&hellip;' : 'Create organization') + '</button></div>';
  }

  function _actionRow(id, icon, label, detail) {
    return '<button type="button" id="' + id + '" data-testid="' + id + '" ' +
      'class="w-full flex items-center gap-3.5 bg-gray-950 border border-gray-800 hover:border-gray-600 rounded-xl p-4 text-left">' +
      '<span class="w-9 h-9 rounded-lg bg-gray-800 flex items-center justify-center flex-shrink-0" aria-hidden="true">' + icon + '</span>' +
      '<span><span class="block text-sm font-medium">' + label + '</span>' +
      '<span class="block text-xs text-gray-500 mt-0.5">' + detail + '</span></span></button>';
  }

  function _doneHtml() {
    var markInner = S.icon
      ? '<img src="' + _esc(S.icon) + '" class="w-full h-full object-cover" alt="">'
      : '<span>' + _esc(deriveOrgInitial(S.name)) + '</span>';
    return '<div data-testid="create-org-success">' +
      '<div class="flex items-center gap-4">' +
      '<span class="w-14 h-14 rounded-xl flex items-center justify-center font-bold text-xl flex-shrink-0 text-white overflow-hidden"' +
      (S.icon ? '' : ' style="background:' + S.color + '"') + ' aria-hidden="true">' + markInner + '</span>' +
      '<h1 class="text-xl font-semibold">' + _esc(S.name) + ' has been created</h1></div>' +
      '<div class="mt-7 space-y-3" data-testid="create-org-next-steps">' +
      _actionRow('create-org-goto-settings', _ICON_GEAR, 'Go to Settings',
        'Organization structure, sharing policies, administration') +
      _actionRow('create-org-create-workspace', _ICON_GRID, 'Create a workspace',
        'A place for sessions and agents to work') +
      _actionRow('create-org-invite', _ICON_INVITE, 'Invite teammates',
        'When you&rsquo;re ready &mdash; invites are links you can send') +
      '</div><div class="mt-8 flex md:justify-end">' +
      '<button type="button" id="create-org-finish" data-testid="create-org-finish" ' +
      'class="w-full md:w-auto px-5 py-2.5 rounded-lg text-sm text-gray-400 hover:text-gray-200 border border-gray-700">' +
      (S.entry === 'onboarding' ? 'Finish' : 'Close') + '</button></div></div>';
  }

  // ── rendering ──────────────────────────────────────────────────────
  //
  // Full render happens on phase changes only. Keystrokes touch just the
  // mark slot and the submit button, so the input never loses focus.

  function _render() {
    var existing = document.getElementById('create-org');
    if (existing) existing.remove();
    if (!S) return;
    var overlay = document.createElement('div');
    overlay.id = 'create-org';
    overlay.setAttribute('data-testid', 'create-org');
    overlay.setAttribute('data-phase', S.phase);
    overlay.className = 'fixed inset-0 z-[70] bg-gray-900 text-gray-100 overflow-y-auto';
    overlay.innerHTML =
      '<div class="min-h-full flex flex-col items-center md:justify-center px-5 py-6 md:p-8">' +
      '<div class="text-lg md:text-xl font-bold text-indigo-400 self-start md:self-center md:mb-6">Autonomy</div>' +
      '<div class="w-full md:max-w-lg flex flex-col flex-1 md:flex-none ' +
      'md:bg-gray-800 md:border md:border-gray-700 md:rounded-2xl md:shadow-2xl md:p-8 ' +
      'pt-4 md:pt-8 pb-[calc(1.5rem+env(safe-area-inset-bottom))] md:pb-8">' +
      (S.phase === 'form' ? _formHtml() : _doneHtml()) +
      '</div></div>';
    document.body.appendChild(overlay);
    _wire(overlay);
  }

  function _updateMark() {
    var slot = document.getElementById('create-org-markslot');
    if (slot) slot.innerHTML = _markHtml();
    var submit = document.getElementById('create-org-submit');
    if (submit) submit.disabled = S.busy || !deriveOrgSlug(S.name);
    _wireMark();
  }

  function _updatePicker() {
    var slot = document.getElementById('create-org-pickerslot');
    if (slot) slot.innerHTML = S.pickerOpen ? _pickerHtml() : '';
    var mark = document.getElementById('create-org-mark');
    if (mark) mark.setAttribute('aria-expanded', S.pickerOpen ? 'true' : 'false');
    _wirePicker();
    _updateMark();
  }

  function _setError(message) {
    S.error = message || '';
    var el = document.getElementById('create-org-error');
    if (el) {
      el.textContent = S.error;
      el.classList.toggle('hidden', !S.error);
    }
  }

  // ── wiring ─────────────────────────────────────────────────────────

  function _wireMark() {
    var mark = document.getElementById('create-org-mark');
    if (mark) {
      mark.addEventListener('click', function (event) {
        event.stopPropagation();
        S.pickerOpen = !S.pickerOpen;
        _updatePicker();
      });
    }
  }

  function _wirePicker() {
    var picker = document.getElementById('create-org-mark-picker');
    if (!picker) return;
    picker.addEventListener('click', function (event) { event.stopPropagation(); });
    picker.querySelectorAll('[data-testid=create-org-swatch]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        S.color = btn.getAttribute('data-color');   // manual pick: pinned for good
        S.pickerOpen = false;
        _updatePicker();
      });
    });
    var input = document.getElementById('create-org-icon-input');
    if (input) {
      input.addEventListener('change', function () {
        var file = input.files && input.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function (e) {
          S.icon = e.target.result;
          S.pickerOpen = false;
          _updatePicker();
        };
        reader.readAsDataURL(file);
      });
    }
    var remove = document.getElementById('create-org-icon-remove');
    if (remove) {
      remove.addEventListener('click', function () {
        S.icon = '';
        S.pickerOpen = false;
        _updatePicker();
      });
    }
  }

  async function _submit() {
    if (S.busy) return;
    var slug = deriveOrgSlug(S.name);
    if (!slug) return;
    S.busy = true;
    _setError('');
    var submit = document.getElementById('create-org-submit');
    if (submit) { submit.disabled = true; submit.innerHTML = 'Creating&hellip;'; }
    try {
      var identity = { name: S.name.trim(), color: S.color, type: 'shared' };
      if (S.icon) identity.favicon = S.icon;
      await _fetchJson('/api/orgs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug: slug, type: 'shared', identity: identity }),
      });
      S.busy = false;
      S.phase = 'done';
      window.dispatchEvent(new Event('autonomy:orgs-changed'));
      _render();
    } catch (e) {
      S.busy = false;
      if (submit) { submit.disabled = false; submit.innerHTML = 'Create organization'; }
      _setError(e.status === 409
        ? 'That name is already in use here — organization names need to be unique.'
        : (e.message || 'Could not create the organization.'));
    }
  }

  function _dismiss() {
    var entry = S && S.entry;
    close();
    if (entry === 'onboarding' && window.AutonomyOnboarding) {
      window.AutonomyOnboarding.open({ step: 3 });
    }
  }

  function _wire(overlay) {
    _wireMark();
    _wirePicker();
    var name = document.getElementById('create-org-name');
    if (name) {
      name.addEventListener('input', function () {
        S.name = name.value;
        if (S.name.trim() && !S.color) S.color = deriveOrgColor(S.name);  // pin once
        _updateMark();
      });
      name.focus();
    }
    var on = function (id, fn) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('click', fn);
    };
    on('create-org-dismiss', _dismiss);
    on('create-org-submit', _submit);
    on('create-org-finish', _dismiss);
    on('create-org-goto-settings', function () { window.location.assign('/settings'); });
    on('create-org-create-workspace', function () { window.location.assign('/settings'); });
    on('create-org-invite', function () {
      var row = overlay.querySelector('[data-testid=create-org-invite] .text-gray-500');
      if (row) row.textContent = 'Invites are coming soon — this organization is ready for them.';
    });
    overlay.addEventListener('click', function (event) {
      if (S && S.pickerOpen && !event.target.closest('#create-org-mark-picker')) {
        S.pickerOpen = false;
        _updatePicker();
      }
    });
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' || !S) return;
      if (S.pickerOpen) {
        S.pickerOpen = false;
        _updatePicker();
      } else if (S.phase === 'form') {
        _dismiss();
      }
    });
  }

  // ── public API ─────────────────────────────────────────────────────

  function open(opts) {
    opts = opts || {};
    S = { entry: opts.entry === 'onboarding' ? 'onboarding' : 'standalone',
          phase: 'form', name: '', color: '', icon: '',
          pickerOpen: false, busy: false, error: '' };
    _render();
  }

  function close() {
    S = null;
    var el = document.getElementById('create-org');
    if (el) el.remove();
  }

  if (typeof window !== 'undefined') {
    window.AutonomyCreateOrg = {
      open: open,
      close: close,
      _internals: {
        deriveOrgSlug: deriveOrgSlug,
        deriveOrgInitial: deriveOrgInitial,
        deriveOrgColor: deriveOrgColor,
        state: function () { return S; },
      },
    };
  }

  if (typeof module === 'object' && module.exports) {
    module.exports = { deriveOrgSlug: deriveOrgSlug,
                      deriveOrgInitial: deriveOrgInitial,
                      deriveOrgColor: deriveOrgColor };
  }
})();
