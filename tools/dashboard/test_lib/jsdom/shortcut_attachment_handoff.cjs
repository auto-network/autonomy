// Signed .shortcut files use a non-navigating Web Share handoff when iOS
// accepts the file type, with a separate-context fallback that never carries
// the download attribute which traps standalone PWAs in Quick Look.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");

const viewerPath = resolve(__dirname, "../../static/js/pages/session-viewer.js");
const entriesPath = resolve(__dirname, "../../templates/partials/session-entries.html");
const pagePath = resolve(__dirname, "../../templates/partials/session-lightbox.html");

const components = {};
const listeners = {};
const anchors = [];
const shareCalls = [];

const Alpine = {
  data(name, factory) { components[name] = factory; },
  store() { return {}; },
};

const document = {
  addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
  querySelector() { return null; },
  body: {
    appendChild(node) { anchors.push(node); node.appended = true; },
  },
  createElement(tagName) {
    assert.equal(tagName, "a");
    return {
      clicked: false,
      removed: false,
      click() { this.clicked = true; },
      remove() { this.removed = true; },
    };
  },
};

class FakeFile {
  constructor(parts, name, opts) {
    this.parts = parts;
    this.name = name;
    this.type = opts.type;
  }
}

const signedBlob = { type: "application/octet-stream" };
const navigator = {
  canShare(payload) { return payload.files[0] instanceof FakeFile; },
  share(payload) { shareCalls.push(payload); return Promise.resolve(); },
};

const sandbox = {
  Alpine,
  Blob,
  File: FakeFile,
  Promise,
  console,
  document,
  navigator,
  fetch: async () => ({ ok: true, blob: async () => signedBlob }),
  setTimeout,
  clearTimeout,
  window: { Alpine, SessionRenderer: {} },
};
sandbox.window.document = document;
sandbox.window.navigator = navigator;
vm.createContext(sandbox);
vm.runInContext(readFileSync(viewerPath, "utf8"), sandbox, { filename: viewerPath });
(listeners["alpine:init"] || []).forEach((callback) => callback());

function settle() {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
}

(async () => {
  const viewer = components.sessionViewerPage({});
  assert.equal(
    viewer.lightboxKindForMime("application/octet-stream", "Autonomy Capture.shortcut"),
    "shortcut",
  );
  assert.equal(
    viewer.lightboxKindForMime("application/octet-stream", "archive.zip"),
    "download",
  );

  viewer.openLightbox("/signed.shortcut", "signed installer", {
    kind: "shortcut",
    name: "Autonomy Capture.shortcut",
  });
  assert.equal(viewer.lightboxFileState, "loading");
  await settle();
  assert.equal(viewer.lightboxFileState, "ready");
  assert.equal(viewer.lightboxFile.name, "Autonomy Capture.shortcut");

  viewer.openShortcutInstaller();
  assert.equal(shareCalls.length, 1, "native share begins in the button click");
  assert.equal(shareCalls[0].files[0], viewer.lightboxFile);
  await settle();
  assert.equal(viewer.lightboxSrc, "", "successful share closes only our sheet");
  assert.equal(anchors.length, 0, "native share does not navigate");

  navigator.canShare = () => false;
  viewer.openLightbox("/signed.shortcut", "signed installer", {
    kind: "shortcut",
    name: "Autonomy Capture.shortcut",
  });
  await settle();
  viewer.openShortcutInstaller();
  assert.equal(anchors.length, 1);
  assert.equal(anchors[0].href, "/signed.shortcut");
  assert.equal(anchors[0].target, "_blank");
  assert.equal(anchors[0].rel, "noopener noreferrer");
  assert.equal("download" in anchors[0], false);
  assert.equal(anchors[0].clicked, true);
  assert.equal(anchors[0].removed, true);

  const entries = readFileSync(entriesPath, "utf8");
  const page = readFileSync(pagePath, "utf8");
  assert.match(entries, /lightboxKindForMime\(resolveEntry\(dEntry\)\.mime, resolveEntry\(dEntry\)\.filename\)/);
  assert.match(page, /data-testid="shortcut-install-sheet"/);
  assert.match(page, /Open in Shortcuts/);

  console.log("shortcut attachment handoff -> PASS");
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
