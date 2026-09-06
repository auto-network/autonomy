// Approved Design Studio revision (design 0e0af1a3, "Organization
// Membership — Settings Screen"): the Charter section materialized as a
// production screen. Only the simulated inputs were replaced — initial
// values come from GET /api/orgs/{slug}, the save is
// PUT /api/orgs/{slug}/charter (autonomy.org#2). Bead auto-bkoe6.
(function () {
  'use strict';
  var api = window.AutonomyOrgSettings;
  if (!api) return;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function request(url, options) {
    options = options || {};
    options.headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
    options.credentials = 'same-origin';
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var error = new Error(body.error || ('HTTP ' + res.status));
          error.status = res.status;
          throw error;
        }
        return body;
      });
    });
  }
  function initial(name) { return (name || '?').trim().charAt(0).toUpperCase(); }

  var FIELDS = ['name', 'byline', 'description', 'favicon', 'color'];

  function Controller(slug, root, identity) {
    this.slug = slug;
    this.root = root;
    // The stored payload is the baseline dirtiness compares against; the
    // schema's own fields only — anything else (e.g. `type`) is preserved
    // untouched on save.
    this.stored = identity && identity.payload ? identity.payload : {};
    this.form = {};
    var self = this;
    FIELDS.forEach(function (f) { self.form[f] = self.stored[f] || ''; });
    this.saved = false;
    this.busy = false;
    this.error = null;
  }

  Controller.prototype.dirty = function () {
    var self = this;
    return FIELDS.some(function (f) {
      return (self.form[f] || '') !== (self.stored[f] || '');
    });
  };

  Controller.prototype.payload = function () {
    var out = {};
    if (this.stored.type) out.type = this.stored.type;
    var self = this;
    FIELDS.forEach(function (f) {
      var value = (self.form[f] || '').trim();
      if (f === 'name' || value) out[f] = f === 'name' ? self.form.name.trim() : value;
    });
    return out;
  };

  Controller.prototype.save = function () {
    var self = this;
    if (this.busy || !this.dirty() || !this.form.name.trim()) return;
    this.busy = true;
    this.error = null;
    this.render();
    request('/api/orgs/' + encodeURIComponent(this.slug) + '/charter', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(this.payload()),
    }).then(function () {
      self.stored = self.payload();
      self.saved = true;
      setTimeout(function () { self.saved = false; if (self.root.isConnected) self.render(); }, 2000);
    }).catch(function (error) {
      self.error = (error && error.message) || String(error);
    }).finally(function () {
      self.busy = false;
      self.render();
    });
  };

  Controller.prototype.reset = function () {
    var self = this;
    FIELDS.forEach(function (f) { self.form[f] = self.stored[f] || ''; });
    this.error = null;
    this.render();
  };

  Controller.prototype.render = function () {
    var f = this.form;
    var dirty = this.dirty();
    var color = f.color || '#6C63FF';
    this.root.innerHTML =
      '<div class="mem-section">' +
        '<div class="mem-heading"><h2>Charter</h2></div>' +
        '<div class="mem-form">' +
          (this.error ? '<p class="mem-error">' + esc(this.error) + '</p>' : '') +
          '<label for="ch-name">Name</label>' +
          '<input id="ch-name" type="text" maxlength="120" data-field="name" value="' + esc(f.name) + '">' +
          '<label for="ch-byline">Subtitle</label>' +
          '<input id="ch-byline" type="text" maxlength="60" data-field="byline" value="' + esc(f.byline) + '">' +
          '<p class="mem-hint"><span data-role="byline-left">' + (60 - (f.byline || '').length) + '</span> characters left — it sits under the name and stays one line on a phone.</p>' +
          '<label for="ch-desc">Long description</label>' +
          '<textarea id="ch-desc" rows="5" maxlength="4000" data-field="description">' + esc(f.description) + '</textarea>' +
          '<label for="ch-favicon">Favicon</label>' +
          '<div class="mem-favicon-row">' +
            '<span class="orgset-org-badge" style="background:' + esc(color) + '">' + esc(initial(f.name)) + '</span>' +
            '<input id="ch-favicon" type="text" data-field="favicon" value="' + esc(f.favicon) + '" placeholder="/static/icon-192.png">' +
          '</div>' +
          '<label for="ch-color">Brand color</label>' +
          '<div class="mem-favicon-row">' +
            '<input id="ch-color" type="color" data-field="color" value="' + esc(/^#[0-9a-fA-F]{6}$/.test(f.color) ? f.color : '#6C63FF') + '" style="min-height:2.3rem;width:3.4rem;padding:.1rem;border:1px solid #4b5563;border-radius:.38rem;background:rgba(3,7,18,.7)">' +
            '<input type="text" class="mono" data-field="color" style="flex:1" value="' + esc(f.color) + '">' +
          '</div>' +
          '<div class="mem-panel-actions" style="margin-top:.7rem">' +
            (dirty ? '<button type="button" class="mem-secondary" data-action="discard">Discard</button>' : '') +
            '<button type="button" class="mem-primary" data-action="save"' +
              ((!dirty || !f.name.trim() || this.busy) ? ' disabled' : '') + '>' +
              (this.saved ? 'Saved' : 'Save charter') + '</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    this.bind();
  };

  Controller.prototype.bind = function () {
    var self = this;
    this.root.querySelectorAll('[data-field]').forEach(function (input) {
      input.addEventListener('input', function () {
        self.form[input.getAttribute('data-field')] = input.value;
        // Re-render only what cheap bindings can't cover: button state,
        // hint count, badge color — without stealing focus mid-typing.
        var hint = self.root.querySelector('[data-role="byline-left"]');
        if (hint) hint.textContent = String(60 - (self.form.byline || '').length);
        var badge = self.root.querySelector('.orgset-org-badge');
        if (badge) {
          badge.style.background = self.form.color || '#6C63FF';
          badge.textContent = initial(self.form.name);
        }
        var save = self.root.querySelector('[data-action="save"]');
        if (save) save.disabled = !self.dirty() || !self.form.name.trim() || self.busy;
        var discard = self.root.querySelector('[data-action="discard"]');
        if (self.dirty() && !discard) self.render();
      });
    });
    var save = this.root.querySelector('[data-action="save"]');
    if (save) save.addEventListener('click', function () { self.save(); });
    var discard = this.root.querySelector('[data-action="discard"]');
    if (discard) discard.addEventListener('click', function () { self.reset(); });
  };

  api.register({
    id: 'charter',
    label: 'Charter',
    windowTitle: 'Charter',
    order: 5,
    render: function (slug) {
      var root = document.createElement('section');
      root.className = 'org-charter';
      root.innerHTML = '<div class="orgset-loading">Loading…</div>';
      return request('/api/orgs/' + encodeURIComponent(slug)).then(function (detail) {
        var controller = new Controller(slug, root, detail.identity || null);
        controller.render();
        return root;
      });
    },
  });
})();
