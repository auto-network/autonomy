const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><body></body>', {
  runScripts: 'dangerously', url: 'https://dashboard.example/session/project/auto-one',
});
const w = dom.window;
let directive;
let scope;
const factories = {};
const navigations = [];
const requests = [];
w.SessionRenderer = {};
w.Alpine = {
  directive(name, fn) { if (name === 'markdown') directive = fn; },
  data(name, fn) { factories[name] = fn; },
  $data() { return scope; },
  store() { return {}; },
};
w.hljs = { highlightElement() {} };
w.navigateTo = href => navigations.push(href);
w.fetch = async (src, opts) => {
  requests.push([src, opts.method || 'GET']);
  return {
    ok: !src.includes('missing'), status: src.includes('missing') ? 404 : 200,
    headers: { get: () => src.endsWith('.png') ? 'image/png' : 'text/markdown; charset=utf-8' },
    text: async () => '# Real report\n\n[Sibling](sibling.md)\n\n[Note](graph://12345678-abc)',
  };
};
function inject(p) {
  w.eval(fs.readFileSync(path.resolve(__dirname, '../../', p), 'utf8'));
}
inject('static/vendor/marked-15.0.12.min.js');
inject('static/vendor/purify-3.4.12.min.js');
inject('static/js/markdown.js');
inject('static/js/pages/session-viewer.js');
w.document.dispatchEvent(new w.Event('alpine:init'));
scope = factories.sessionViewerPage({ mode: 'panel' });
scope.sessionKey = 'auto-one';
function render(md, preview = false) {
  const el = w.document.createElement('div');
  if (preview) el.classList.add('sc-va-lightbox-md');
  w.document.body.appendChild(el);
  directive(el, { expression: 'message', modifiers: [] }, {
    effect: fn => fn(), evaluate: () => md,
  });
  return el;
}
function click(a, extra = {}) {
  return a.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...extra }));
}
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
(async () => {
  const el = render([
    '[Report](</workspace/output/nested/My report.md>)',
    '[Missing](/workspace/output/missing.md)',
    '[Image](/workspace/output/proof.png)',
    '[Note](graph://12345678-abc)',
    '[Design](https://b9d69a31ccbc.tail35c24e.ts.net:8080/design/326122bd-f3a2-4938-93fa-0510403a53a5?x=1#review)',
    '[External](https://example.org/design/12345678-abc)',
    '[API](/api/health)',
    '[Peer report](https://b9d69a31ccbc.tail35c24e.ts.net:8080/api/session/auto-peer/output/report.md)',
    '[Traversal](/workspace/output/%2e%2e/private.md)',
    '[Hash](#local)',
    '[Nested **label**](graph://12345678-abc)',
  ].join('\n\n'));
  const link = label => [...el.querySelectorAll('a')].find(a => a.textContent === label);
  assert.equal(link('Report').getAttribute('href'), '/api/session/auto-one/output/nested/My%20report.md');
  assert.equal(link('Peer report').getAttribute('href'), '/api/session/auto-peer/output/report.md', 'explicit peer ownership survives alias normalization');
  click(link('Report'));
  await settle();
  assert.equal(scope.lightboxKind, 'markdown');
  assert.match(scope.lightboxMarkdown, /^# Real report/);
  assert.equal(navigations.length, 0, 'files do not go through the page router');
  const preview = render(scope.lightboxMarkdown, true);
  assert.equal(preview.querySelector('a').getAttribute('href'), '/api/session/auto-one/output/nested/sibling.md');
  click(link('Note'));
  assert.equal(navigations.pop(), '/graph/12345678-abc');
  assert.equal(scope.lightboxSrc, '', 'internal navigation closes the preview');
  click(link('Design'));
  assert.equal(navigations.pop(), '/design/326122bd-f3a2-4938-93fa-0510403a53a5?x=1&from_session=auto-one#review');
  assert.equal(link('External').getAttribute('target'), '_blank');
  assert.equal(link('Traversal').hasAttribute('href'), false);
  assert.equal(link('Hash').getAttribute('href'), '#local');
  assert.equal(el.querySelectorAll('a a').length, 0, 'formatted labels do not acquire nested graph links');
  click(link('API'));
  assert.equal(navigations.length, 0, 'raw API links are not SPA pages');
  click(link('Missing'));
  await settle();
  assert.match(scope.lightboxMarkdown, /404/);
  click(link('Image'));
  await settle();
  assert.equal(scope.lightboxKind, 'image');
  assert.equal(scope.lightboxSrc, '/api/session/auto-one/output/proof.png');
  scope.closeLightbox();
  const before = requests.length;
  click(link('Report'), { ctrlKey: true });
  assert.equal(requests.length, before, 'modified clicks retain browser semantics');
  scope.sessionKey = 'auto-two';
  assert.equal(render('[R](/workspace/output/a.md)').querySelector('a').getAttribute('href'), '/api/session/auto-two/output/a.md');
  assert.equal(render('[D](/design/12345678-abc?from_session=auto-old#review)').querySelector('a').getAttribute('href'), '/design/12345678-abc?from_session=auto-two#review', 'return control follows the session the link was actually opened from');
  scope.sessionKey = '';
  assert.equal(render('[D](/design/12345678-abc)').querySelector('a').getAttribute('href'), '/design/12345678-abc', 'non-session surfaces keep the gallery entry');
  scope.sessionKey = 'auto-two';
  // Closing the sheet while HEAD is outstanding must not reopen it.
  let release;
  w.fetch = () => new Promise(resolve => { release = resolve; });
  const pending = scope.openOutputLink('/api/session/auto-two/output/slow.md');
  scope.closeLightbox();
  release({ ok: true, headers: { get: () => 'text/markdown' } });
  await pending;
  assert.equal(scope.lightboxSrc, '');
  const template = fs.readFileSync(path.resolve(__dirname, '../../templates/partials/session-lightbox.html'), 'utf8');
  assert.match(template, /x-teleport="body"/);
  assert.match(template, /x-markdown="lightboxMarkdown"/);
  assert.match(template, /sandbox="allow-scripts"/);
  dom.window.close();
  console.log('markdown artifact links -> PASS');
})().catch(err => { console.error(err); process.exitCode = 1; dom.window.close(); });
