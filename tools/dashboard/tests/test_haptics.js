// #35 — reusable web-haptics helper (the iOS 17.4+ <input type=checkbox switch>
// trick). Verifies the public API, the hidden-switch lifecycle, and that it's a
// safe no-op when unsupported.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const HAPTICS_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/haptics.js');

function load(opts) {
  const appended = [];
  const clicks = [];
  function makeEl() {
    return {
      _attrs: {}, style: {}, tabIndex: 0, type: '',
      setAttribute(k, v) { this._attrs[k] = v; },
      getAttribute(k) { return this._attrs[k]; },
      click() { clicks.push(this); },
    };
  }
  const document = {
    body: (opts && opts.noBody) ? null : { appendChild(el) { appended.push(el); } },
    createElement() { return makeEl(); },
  };
  const sandbox = { window: {}, document, console };
  sandbox.window.document = document;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(HAPTICS_JS, 'utf8'), sandbox, { filename: 'haptics.js' });
  return { window: sandbox.window, appended, clicks };
}

describe('#35 haptics helper', () => {
  it('exposes window.Autonomy.haptic', () => {
    assert.equal(typeof load().window.Autonomy.haptic, 'function');
  });

  it('creates one hidden switch input and clicks it on haptic()', () => {
    const h = load();
    h.window.Autonomy.haptic();
    assert.equal(h.appended.length, 1, 'one switch element created');
    const el = h.appended[0];
    assert.equal(el.type, 'checkbox');
    assert.equal(el.getAttribute('switch'), '', 'iOS switch attribute set');
    assert.equal(el.getAttribute('aria-hidden'), 'true');
    assert.equal(h.clicks.length, 1, 'clicked once to emit the tap');
  });

  it('reuses the same switch element across calls (no second element created)', () => {
    const h = load();
    h.window.Autonomy.haptic();
    h.window.Autonomy.haptic();
    assert.equal(h.appended.length, 1, 'element reused, not recreated');
  });

  it('debounces near-simultaneous calls into a single tap', () => {
    const h = load();
    h.window.Autonomy.haptic();
    h.window.Autonomy.haptic();   // same millisecond → collapsed
    assert.equal(h.clicks.length, 1, 'rapid double-call fires once');
  });

  it('is a safe no-op when there is no document.body yet', () => {
    const h = load({ noBody: true });
    assert.doesNotThrow(() => h.window.Autonomy.haptic());
    assert.equal(h.clicks.length, 0);
  });
});
