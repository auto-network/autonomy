// Shared organization identity picker. Pages own fetching, scope and persistence.
// Plain JS: OrgPicker.mount(el, {orgs, value, onChange}) → update / destroy.
// Alpine: x-org-picker="{orgs, value: selectedOrg, onChange: slug => setOrg(slug)}"
(function (window) {
  'use strict';
  var document = window.document, active = null, sequence = 0;
  function initial(value) { return Array.from(String(value || '?').trim().toUpperCase())[0] || '?'; }
  function iconUrl(value) {
    if (!value || typeof value !== 'string') return null;
    try {
      var url = new URL(value, document.baseURI);
      return /^(https?):$/.test(url.protocol) ? value : null;
    } catch (_) { return null; }
  }
  function normalize(rows) {
    var seen = new Set();
    return (Array.isArray(rows) ? rows : []).filter(Boolean).map(function (entry) {
      var base = entry.org || entry;
      var id = entry.identity_resolved || (entry.identity && entry.identity.payload) || entry;
      var slug = String(base.slug || id.slug || '');
      var name = String(id.name || slug);
      return Object.assign({}, entry, {
        slug: slug, name: name, color: id.color || '#64748b',
        initial: initial(id.initial || name),
        favicon: iconUrl(id.favicon), detail: String(entry.detail || entry.kind || ''),
      });
    }).filter(function (row) {
      if (!row.slug || seen.has(row.slug)) return false;
      seen.add(row.slug); return true;
    });
  }
  function node(tag, cls, text) {
    var el = document.createElement(tag); el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function glyph(org) {
    var mark = node('span', 'org-picker-glyph');
    mark.setAttribute('aria-hidden', 'true');
    mark.style.backgroundColor = org.color;
    var initial = node('span', 'org-picker-initial', org.initial);
    mark.append(initial);
    if (org.favicon) {
      var img = node('img', 'org-picker-image'); img.alt = ''; img.referrerPolicy = 'no-referrer';
      img.hidden = true;
      img.onload = function () { initial.hidden = true; img.hidden = false; mark.style.backgroundColor = 'transparent'; };
      img.onerror = function () { initial.hidden = false; mark.style.backgroundColor = org.color; img.remove(); };
      img.src = org.favicon; mark.append(img);
    }
    return mark;
  }
  function Control(el, opts) {
    this.el = el; this.opts = {}; this.destroyed = false;
    this.trigger = node('button', 'org-picker-trigger'); this.trigger.type = 'button';
    this.menu = node('div', 'org-picker-menu'); this.menu.hidden = true;
    this.menu.id = 'org-picker-menu-' + (++sequence);
    this.menu.setAttribute('role', 'listbox'); this.menu.setAttribute('aria-label', 'Organization');
    this.trigger.setAttribute('aria-haspopup', 'listbox');
    this.trigger.setAttribute('aria-controls', this.menu.id);
    this.trigger.setAttribute('aria-expanded', 'false');
    el.classList.add('org-picker'); el.replaceChildren(this.trigger);
    document.body.append(this.menu);
    var self = this;
    this.onClick = function () { self.menu.hidden ? self.open() : self.close(); };
    this.onKey = function (event) { self.key(event); };
    this.onOutside = function (event) {
      if (!el.contains(event.target) && !self.menu.contains(event.target)) self.close();
    };
    this.onPosition = function () { if (!self.menu.hidden) self.position(); };
    this.trigger.addEventListener('click', this.onClick);
    this.trigger.addEventListener('keydown', this.onKey);
    this.menu.addEventListener('keydown', this.onKey);
    this.update(opts);
  }
  Control.prototype.update = function (opts) {
    if (this.destroyed) return;
    Object.assign(this.opts, opts || {});
    this.orgs = normalize(this.opts.orgs);
    this.value = String(this.opts.value || '');
    this.choices = this.orgs.slice();
    var all = {slug:'',name:this.opts.allLabel || 'All organizations',initial:'∞',color:'#334155',favicon:null};
    if (this.opts.allowAll) this.choices.unshift(all);
    var selected = this.choices.find(function (o) { return o.slug === this.value; }, this);
    if (!selected) selected = {slug:this.value,name:this.value || this.opts.placeholder || 'Choose organization',initial:initial(this.value),color:'#64748b'};
    var signature = JSON.stringify([selected, !!this.opts.iconOnly]);
    if (signature !== this.triggerSignature) {
      this.triggerSignature = signature;
      this.trigger.replaceChildren(glyph(selected), node('span', 'org-picker-label', selected.name), node('span', 'org-picker-chevron', '▾'));
    }
    this.trigger.classList.toggle('is-icon-only', !!this.opts.iconOnly);
    this.trigger.classList.toggle('is-compact-mobile', !!this.opts.compactOnMobile);
    this.el.classList.toggle('is-compact-mobile', !!this.opts.compactOnMobile);
    this.trigger.title = selected.name;
    this.trigger.setAttribute('aria-label', 'Organization: ' + selected.name);
    this.trigger.disabled = this.orgs.length === 0;
    if (this.opts.testId) this.trigger.dataset.testid = this.opts.testId;
    if (this.opts.menuTestId) this.menu.dataset.testid = this.opts.menuTestId;
    var menuSignature = JSON.stringify([this.choices, this.value, this.opts.optionTestPrefix]);
    if (menuSignature !== this.menuSignature) {
      this.menuSignature = menuSignature;
      var focused = this.menu.contains(document.activeElement) ? document.activeElement.dataset.slug : null;
      var self = this;
      this.buttons = this.choices.map(function (org) {
        var button = node('button', 'org-picker-option'); button.type = 'button';
        button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(org.slug === self.value));
        button.tabIndex = -1; button.dataset.slug = org.slug;
        if (self.opts.optionTestPrefix) button.dataset.testid = self.opts.optionTestPrefix + (org.slug || 'all');
        var copy = node('span', 'org-picker-copy'); copy.append(node('span', 'org-picker-name', org.name));
        if (org.detail) copy.append(node('span', 'org-picker-detail', org.detail));
        var check = node('span', 'org-picker-check', org.slug === self.value ? '✓' : ''); check.setAttribute('aria-hidden','true');
        button.append(glyph(org), copy, check);
        button.addEventListener('click', function () {
          self.close(true);
          if (org.slug !== self.value && typeof self.opts.onChange === 'function') self.opts.onChange(org.slug);
        });
        return button;
      });
      this.menu.replaceChildren.apply(this.menu, this.buttons);
      if (!this.menu.hidden && focused !== null) this.focus(this.buttons.findIndex(function (b) { return b.dataset.slug === focused; }));
    }
    if (this.trigger.disabled) this.close();
    else if (!this.menu.hidden) this.position();
  };
  Control.prototype.focus = function (index) {
    if (index < 0) index = this.choices.findIndex(function (o) { return o.slug === this.value; }, this);
    index = Math.max(0, Math.min(index, this.buttons.length - 1));
    this.buttons.forEach(function (b, i) { b.tabIndex = i === index ? 0 : -1; });
    if (this.buttons[index]) this.buttons[index].focus({preventScroll:true});
  };
  Control.prototype.open = function () {
    if (this.destroyed || this.trigger.disabled) return;
    if (active && active !== this) active.close();
    active = this; this.menu.hidden = false; this.trigger.setAttribute('aria-expanded','true');
    document.addEventListener('pointerdown', this.onOutside, true);
    window.addEventListener('resize', this.onPosition);
    window.addEventListener('scroll', this.onPosition, true);
    this.position(); this.focus(-1);
  };
  Control.prototype.close = function (restore) {
    var focusedInside = this.menu.contains(document.activeElement);
    this.menu.hidden = true; this.trigger.setAttribute('aria-expanded','false');
    document.removeEventListener('pointerdown', this.onOutside, true);
    window.removeEventListener('resize', this.onPosition);
    window.removeEventListener('scroll', this.onPosition, true);
    if (active === this) active = null;
    if (restore || focusedInside) this.trigger.focus({preventScroll:true});
  };
  Control.prototype.key = function (event) {
    var key = event.key;
    if (key === 'Tab' && !this.menu.hidden) { this.close(true); return; }
    if (key === 'Escape' && !this.menu.hidden) { event.preventDefault(); event.stopPropagation(); this.close(true); return; }
    if (!['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(key)) return;
    if (this.menu.hidden) {
      if (key === 'ArrowDown' || key === 'ArrowUp') { event.preventDefault(); this.open(); if (key === 'ArrowUp') this.focus(this.buttons.length-1); }
      return; // Native button Enter/Space opens the trigger.
    }
    event.preventDefault(); event.stopPropagation();
    var index = this.buttons.indexOf(document.activeElement);
    if (key === 'Enter' || key === ' ') { if (index >= 0) this.buttons[index].click(); return; }
    if (key === 'Home') index = 0;
    else if (key === 'End') index = this.buttons.length - 1;
    else index = (index + (key === 'ArrowDown' ? 1 : -1) + this.buttons.length) % this.buttons.length;
    this.focus(index);
    if (this.buttons[index] && this.buttons[index].scrollIntoView) this.buttons[index].scrollIntoView({block:'nearest'});
  };
  Control.prototype.position = function () {
    if (!this.el.isConnected || !this.trigger.getClientRects().length) { this.close(); return; }
    var r = this.trigger.getBoundingClientRect(), gap = 6, margin = 8;
    if (r.bottom <= 0 || r.top >= window.innerHeight || r.right <= 0 || r.left >= window.innerWidth) { this.close(); return; }
    var width = Math.min(280, window.innerWidth - margin*2);
    this.menu.style.width = width + 'px';
    this.menu.style.left = Math.max(margin, Math.min(r.right - width, window.innerWidth - width - margin)) + 'px';
    var below = window.innerHeight - r.bottom - gap - margin, above = r.top - gap - margin;
    var down = below >= Math.min(320, this.menu.scrollHeight) || below >= above;
    this.menu.style.maxHeight = Math.max(0, Math.min(360, down ? below : above)) + 'px';
    this.menu.style.top = (down ? r.bottom + gap : Math.max(margin, r.top - gap - this.menu.getBoundingClientRect().height)) + 'px';
  };
  Control.prototype.destroy = function () {
    if (this.destroyed) return;
    this.close(); this.destroyed = true;
    this.trigger.removeEventListener('click', this.onClick); this.trigger.removeEventListener('keydown', this.onKey);
    this.menu.removeEventListener('keydown', this.onKey); this.menu.remove(); this.trigger.remove();
  };
  window.OrgPicker = {normalize:normalize, mount:function (el, opts) { return new Control(el, opts); }};
  document.addEventListener('alpine:init', function () {
    window.Alpine.directive('org-picker', function (el, binding, utilities) {
      var handle = null, disposed = false, evaluate = utilities.evaluateLater(binding.expression);
      utilities.effect(function () { evaluate(function (opts) {
        if (disposed) return;
        if (handle) handle.update(opts); else handle = window.OrgPicker.mount(el, opts);
      }); });
      utilities.cleanup(function () { disposed = true; if (handle) handle.destroy(); });
    });
  });
})(window);
