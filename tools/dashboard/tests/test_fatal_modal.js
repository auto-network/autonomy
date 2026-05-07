/**
 * Fatal-class toast escalates to a blocking full-screen dialog.
 *
 * Covers the new ``showToast(msg, 'fatal')`` branch in
 * ``tools/dashboard/static/app.js``: when the page is in an
 * unrecoverable state (e.g. initial worktrees load failed), the
 * usual 8-second toast scrolls away and leaves a blank surface.
 * The fatal branch renders a non-dismissable modal with a Refresh
 * button instead.
 *
 * We run the relevant slice of ``app.js`` in a Node ``vm`` sandbox
 * with a hand-rolled DOM that captures element creation + event
 * wiring — same pattern as the existing JS tests under this
 * directory.
 *
 * Run: node --test tools/dashboard/tests/test_fatal_modal.js
 */
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const APP_JS = path.join(REPO_ROOT, 'tools/dashboard/static/app.js');


// ── Minimal DOM ────────────────────────────────────────────────────

function makeElement(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    attributes: Object.create(null),
    style: {},
    classList: {
      _set: new Set(),
      add(...names) { names.forEach(n => this._set.add(n)); },
      contains(name) { return this._set.has(name); },
      remove(name) { this._set.delete(name); },
    },
    _listeners: Object.create(null),
    _focused: false,
    _id: '',
    get id() { return this._id; },
    set id(v) { this._id = String(v); },
    set className(v) {
      this._className = String(v);
      this.classList._set = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get className() { return this._className || ''; },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
      if (name === 'id') this.id = String(value);
    },
    getAttribute(name) {
      return name in this.attributes ? this.attributes[name] : null;
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      child.parentNode = null;
      return child;
    },
    remove() {
      if (this.parentNode) this.parentNode.removeChild(this);
    },
    addEventListener(name, cb) {
      (this._listeners[name] ||= []).push(cb);
    },
    set onclick(cb) {
      this._listeners.click = [cb];
    },
    get onclick() {
      return (this._listeners.click || [])[0] || null;
    },
    fire(name, ev) {
      for (const cb of (this._listeners[name] || [])) cb(ev || {});
    },
    focus() { this._focused = true; },
    querySelector(selector) {
      // Minimal selector: '.classname' only — the production code
      // looks up '.fatal-modal-message' which is a single class.
      const m = String(selector).match(/^\.(.+)$/);
      if (!m) return null;
      const want = m[1];
      function walk(node) {
        for (const c of node.children) {
          if (c.classList && c.classList.contains(want)) return c;
          const found = walk(c);
          if (found) return found;
        }
        return null;
      }
      return walk(this);
    },
    get textContent() { return this._text || ''; },
    set textContent(v) { this._text = String(v == null ? '' : v); },
  };
  return el;
}


function makeDocument() {
  const body = makeElement('body');
  const byId = Object.create(null);
  const doc = {
    body,
    _byId: byId,
    createElement(tag) { return makeElement(tag); },
    getElementById(id) {
      // Walk body to find a matching id (created elements set id on themselves).
      function walk(node) {
        if (node.id === id) return node;
        for (const c of node.children) {
          const found = walk(c);
          if (found) return found;
        }
        return null;
      }
      return walk(body);
    },
  };
  return doc;
}


function makeWindow(doc) {
  return {
    document: doc,
    location: {
      _reloaded: 0,
      reload() { this._reloaded += 1; },
    },
  };
}


// ── Snippet extraction ─────────────────────────────────────────────


// Pull just the showToast section out of app.js so we don't have to
// load the whole 2200-line module (which has heavy top-level
// side-effects like DOM lookups, SSE, Alpine bootstrapping, etc.).
function extractToastSnippet() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const startMark = '// ── Toast notifications ──';
  const endMark = '// ── Dispatcher state watcher';
  const startIdx = src.indexOf(startMark);
  const endIdx = src.indexOf(endMark);
  if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx) {
    throw new Error('failed to locate toast section in app.js');
  }
  return src.slice(startIdx, endIdx);
}


function runSnippetInSandbox() {
  const doc = makeDocument();
  // Toasts target #toast-container. Mount one so the regular branch
  // appends into it instead of bailing.
  const container = makeElement('div');
  container.id = 'toast-container';
  doc.body.appendChild(container);

  const win = makeWindow(doc);
  const sandbox = {
    window: win,
    document: doc,
    setTimeout: () => 0,
    clearTimeout: () => {},
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(extractToastSnippet(), sandbox, { filename: 'app.js#toast' });
  return { sandbox, win, doc, container };
}


// ── Tests ──────────────────────────────────────────────────────────


describe('showToast(..., "fatal") escalates to a full-screen modal', () => {
  it('renders a backdrop with the error message and a Refresh button', () => {
    const { sandbox, doc } = runSnippetInSandbox();
    sandbox.showToast('Worktree refresh failed: 503 service unavailable', 'fatal');

    const backdrop = doc.getElementById('fatal-modal-backdrop');
    assert.ok(backdrop, 'fatal modal backdrop must be appended to <body>');
    assert.equal(backdrop.getAttribute('role'), 'alertdialog');
    assert.equal(backdrop.getAttribute('aria-modal'), 'true');
    assert.equal(backdrop.getAttribute('data-testid'), 'fatal-modal');

    const msg = backdrop.querySelector('.fatal-modal-message');
    assert.ok(msg, 'fatal modal must render the message');
    assert.equal(msg.textContent, 'Worktree refresh failed: 503 service unavailable');
    assert.equal(msg.getAttribute('data-testid'), 'fatal-modal-message');

    const button = backdrop.querySelector('.fatal-modal-button');
    assert.ok(button, 'fatal modal must render a Refresh button');
    assert.equal(button.tagName, 'BUTTON');
    assert.equal(button.textContent, 'Refresh');
    assert.equal(button.getAttribute('data-testid'), 'fatal-modal-refresh');
  });

  it('Refresh button click reloads the page', () => {
    const { sandbox, doc, win } = runSnippetInSandbox();
    sandbox.showToast('boom', 'fatal');

    const button = doc.getElementById('fatal-modal-backdrop')
      .querySelector('.fatal-modal-button');
    assert.equal(win.location._reloaded, 0);
    button.fire('click');
    assert.equal(win.location._reloaded, 1, 'click → location.reload()');
  });

  it('a second fatal call updates the message instead of stacking', () => {
    const { sandbox, doc } = runSnippetInSandbox();
    sandbox.showToast('first error', 'fatal');
    sandbox.showToast('second error', 'fatal');

    // Walk the body's children and count fatal backdrops; only one.
    const backdrops = (doc.body.children || []).filter(
      c => c.id === 'fatal-modal-backdrop',
    );
    assert.equal(backdrops.length, 1, 'second call must update, not stack');

    const msg = backdrops[0].querySelector('.fatal-modal-message');
    assert.equal(msg.textContent, 'second error');
  });

  it('does NOT append into #toast-container — the fatal branch is independent', () => {
    const { sandbox, container } = runSnippetInSandbox();
    sandbox.showToast('boom', 'fatal');
    assert.equal(
      container.children.length, 0,
      'fatal toast must not also drop a transient toast in the container',
    );
  });

  it('window.showFatalModal is exposed for direct callers', () => {
    const { sandbox, win, doc } = runSnippetInSandbox();
    assert.equal(typeof win.showFatalModal, 'function');
    win.showFatalModal('via direct exposure');

    const msg = doc.getElementById('fatal-modal-backdrop')
      .querySelector('.fatal-modal-message');
    assert.equal(msg.textContent, 'via direct exposure');
  });
});


describe('regular toast paths still behave the same', () => {
  it('error-class messages append into #toast-container', () => {
    const { sandbox, doc, container } = runSnippetInSandbox();
    sandbox.showToast('a regular error', 'error');

    assert.equal(container.children.length, 1);
    const toast = container.children[0];
    assert.ok(toast.classList.contains('toast'));
    assert.ok(toast.classList.contains('toast-error'));
    assert.equal(toast.textContent, 'a regular error');

    // No fatal modal got created for a regular error.
    assert.equal(doc.getElementById('fatal-modal-backdrop'), null);
  });

  it('warning-class messages get the toast-warning class', () => {
    const { sandbox, container } = runSnippetInSandbox();
    sandbox.showToast('heads up', 'warning');
    assert.ok(container.children[0].classList.contains('toast-warning'));
  });

  it('default type is treated as error', () => {
    const { sandbox, container } = runSnippetInSandbox();
    sandbox.showToast('no type');
    assert.ok(container.children[0].classList.contains('toast-error'));
  });
});
