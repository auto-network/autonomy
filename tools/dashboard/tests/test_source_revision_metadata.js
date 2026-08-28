const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const SOURCE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/source.js');

function makePage() {
  let alpineInit = null;
  let factory = null;
  const copied = [];
  const sandbox = {
    console, URL, Date, Math, Number, Object, Array, String, Set,
    navigator: { clipboard: { writeText(value) { copied.push(value); } } },
    localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout() { return 1; }, clearTimeout() {},
    document: {
      addEventListener(type, callback) { if (type === 'alpine:init') alpineInit = callback; },
    },
    Alpine: { data(name, callback) { if (name === 'sourcePage') factory = callback; } },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SOURCE_JS, 'utf8'), sandbox, {
    filename: 'source.js',
  });
  alpineInit();
  const page = factory();
  page.isNote = true;
  page.src = { id: '12345678-90ab-cdef' };
  return { page, copied };
}

describe('graph note revision metadata', () => {
  it('shows revision one instead of treating it as absent', () => {
    const { page } = makePage();
    page.noteVersionCount = 1;
    assert.equal(page.noteVersion, 1);
  });

  it('copies the revision-addressed graph reference and flashes in place', () => {
    const { page, copied } = makePage();
    page.noteVersionCount = 7;
    assert.equal(page.copyGraphVersion(), true);
    assert.deepEqual(copied, ['graph://12345678-90a@7']);
    assert.equal(page.copiedGraphPart, 'version');
  });
});
