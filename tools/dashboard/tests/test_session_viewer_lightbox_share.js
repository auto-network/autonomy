/**
 * Every viewer attachment can leave the overlay as a file (Save / Share).
 *
 * A shared shell script opened as a text preview in a frame the server
 * refused to embed: a blank white panel with no way out. Now every kind is
 * prefetched as a File, and saveLightboxFile() hands it to navigator.share
 * inside the tap (iOS offers Save to Files there), falling back to opening
 * the file in a new context when the browser cannot share files.
 *
 * Run: node --test tools/dashboard/tests/test_session_viewer_lightbox_share.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');
const LIGHTBOX_HTML = path.join(REPO_ROOT, 'tools/dashboard/templates/partials/session-lightbox.html');
const SRC = '/api/session/auto-x/output/.attachments/20260926-1/autonomy-setup.sh';

function flush() { return new Promise((r) => setTimeout(r, 0)); }

function load({ canShare = true } = {}) {
  const components = {};
  const shared = [];
  const anchors = [];
  class FakeFile {
    constructor(parts, name, opts) { this.parts = parts; this.name = name; this.type = opts.type; }
  }
  const document = {
    addEventListener(name, cb) { if (name === 'alpine:init') this._init = cb; },
    removeEventListener() {},
    querySelector() { return null; },
    createElement() {
      const a = { click() { anchors.push({ href: a.href, target: a.target }); }, remove() {} };
      return a;
    },
    body: { appendChild() {} },
  };
  const navigator = canShare
    ? { share(data) { shared.push(data); return Promise.resolve(); }, canShare() { return true; } }
    : {};
  const sandbox = {
    window: { SessionRenderer: {}, addEventListener() {}, removeEventListener() {} },
    document, navigator, File: FakeFile, console, setTimeout, clearTimeout,
    setInterval, clearInterval, Promise, JSON, Object, Array, Map, Set, Date, Error,
    Alpine: { data(n, f) { components[n] = f; }, store() { return {}; } },
    atob: (b) => Buffer.from(b, 'base64').toString('binary'), TextEncoder, Uint8Array,
    // The page's connect-src refuses data: URIs; so does this fake.
    fetch: (url) => String(url).startsWith('data:') ? Promise.reject(new TypeError('blocked by CSP')) : Promise.resolve({
      ok: true, blob: () => Promise.resolve({ type: 'application/x-sh' }),
      text: () => Promise.resolve('# md'),
    }),
  };
  sandbox.window.document = document;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: 'session-viewer.js' });
  if (document._init) document._init();
  const viewer = components.sessionViewerPage({});
  return { viewer, shared, anchors };
}

describe('viewer attachment Save / Share', () => {
  it('prefetches a text attachment under its own name and shares exactly that file', async () => {
    const { viewer, shared } = load();
    viewer.openLightbox(SRC, 'script', { kind: viewer.lightboxKindForMime('text/x-sh', 'autonomy-setup.sh'), name: 'autonomy-setup.sh' });
    assert.equal(viewer.lightboxKind, 'iframe');
    assert.equal(viewer.lightboxFileState, 'loading');
    await flush(); await flush();
    assert.equal(viewer.lightboxFileState, 'ready');
    assert.equal(viewer.lightboxFile.name, 'autonomy-setup.sh');
    viewer.saveLightboxFile();
    assert.equal(shared.length, 1);
    assert.equal(shared[0].files[0], viewer.lightboxFile);
  });

  it('images and downloads are shareable too, named from the URL when no name is given', async () => {
    const { viewer, shared } = load();
    viewer.openLightbox('/api/session/auto-x/output/.attachments/1/shot.png', 'a screenshot');
    await flush(); await flush();
    assert.equal(viewer.lightboxFile.name, 'shot.png');
    viewer.saveLightboxFile();
    assert.equal(shared.length, 1);
  });

  it('markdown loading does not cancel the share prefetch', async () => {
    const { viewer } = load();
    viewer.openLightbox('/api/session/auto-x/output/notes.md', 'notes', { kind: 'markdown', name: 'notes.md' });
    await flush(); await flush();
    assert.equal(viewer.lightboxFileState, 'ready');
    assert.equal(viewer.lightboxMarkdown, '# md');
  });

  it('without file sharing, Save / Share opens the file in a new context', async () => {
    const { viewer, anchors } = load({ canShare: false });
    viewer.openLightbox(SRC, 'script', { kind: 'download', name: 'autonomy-setup.sh' });
    await flush(); await flush();
    viewer.saveLightboxFile();
    assert.equal(anchors.length, 1);
    assert.equal(anchors[0].href, SRC);
    assert.equal(anchors[0].target, '_blank');
  });

  it('the overlay template offers Save / Share on every kind but the Shortcut sheet', () => {
    const html = fs.readFileSync(LIGHTBOX_HTML, 'utf8');
    assert.match(html, /x-if="lightboxKind !== 'shortcut'"[\s\S]*data-testid="lightbox-save-share"[\s\S]*saveLightboxFile\(\)/);
  });
});

describe('viewer attachment video', () => {
  it('video/* opens the inline player and is not prefetched', async () => {
    const { viewer, anchors } = load();
    assert.equal(viewer.lightboxKindForMime('video/mp4', 'walk.mp4'), 'video');
    viewer.openLightbox('/api/session/auto-x/output/walk.mp4', 'walk', { kind: 'video', name: 'walk.mp4' });
    await flush(); await flush();
    assert.equal(viewer.lightboxFile, null);
    assert.notEqual(viewer.lightboxFileState, 'loading');
    viewer.saveLightboxFile();
    assert.equal(anchors.length, 1);
    assert.equal(anchors[0].href, '/api/session/auto-x/output/walk.mp4');
  });

  it('the template renders an inline, inline-playing video element', () => {
    const html = fs.readFileSync(LIGHTBOX_HTML, 'utf8');
    assert.match(html, /x-if="lightboxKind === 'video'"[\s\S]*<video[^>]*controls[^>]*playsinline/);
  });
});

describe('viewer attachment video overlay', () => {
  it('a backdrop tap does not close a video; other kinds still close on tap', () => {
    const html = fs.readFileSync(LIGHTBOX_HTML, 'utf8');
    assert.match(html, /@click="lightboxKind !== 'video' && closeLightbox\(\)"/);
  });

  // Operator's phone, 2026-09-27: a "Read image" tile (an image embedded in
  // the entry as a data: URI) showed "This file could not be prepared for
  // sharing", and Save / Share opened an empty Safari view.
  const PNG = 'data:image/png;base64,' + Buffer.from('PNGBYTES').toString('base64');

  it('prepares an embedded data: image without a fetch, named by its type', () => {
    const { viewer, shared } = load();
    viewer.openLightbox(PNG, 'Read image');
    assert.equal(viewer.lightboxFileState, 'ready');
    assert.equal(viewer.lightboxFileError, '');
    assert.equal(viewer.lightboxFile.name, 'image.png');
    assert.equal(viewer.lightboxFile.type, 'image/png');
    assert.equal(Buffer.from(viewer.lightboxFile.parts[0]).toString(), 'PNGBYTES');
    viewer.saveLightboxFile();
    assert.equal(shared.length, 1);
    assert.equal(shared[0].files[0], viewer.lightboxFile);
  });

  it('never navigates to a data: URI when sharing is unavailable', () => {
    const { viewer, anchors } = load({ canShare: false });
    viewer.openLightbox(PNG, 'Read image');
    viewer.saveLightboxFile();
    assert.equal(anchors.length, 0);
    assert.match(viewer.lightboxFileError, /Press and hold the image/);
  });
});
