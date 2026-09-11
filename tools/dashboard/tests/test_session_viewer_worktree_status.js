const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VIEWER_JS = path.join(
  REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js',
);

function viewerFactory() {
  const listeners = {};
  const components = {};
  const sandbox = {
    window: {},
    document: {
      body: { dataset: {}, classList: { add() {}, remove() {} } },
      addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    },
    Alpine: {
      data(name, factory) { components[name] = factory; },
      store() { return {}; },
    },
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    URLSearchParams, JSON, Object, Array, Date, Math,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, {
    filename: VIEWER_JS,
  });
  (listeners['alpine:init'] || []).forEach((callback) => callback());
  return components.sessionViewerPage;
}

test('a mounted viewer applies pushed worktree rows without reload', () => {
  const viewer = viewerFactory()({});
  viewer.sessionKey = 'auto-live';
  viewer._workspaceHandler = (rows) => viewer._applyWorkspaceRows(rows);

  viewer._workspaceHandler([{
    session_name: 'auto-live', is_dirty: false, dirty_count: 0,
    commits_ahead: 0,
  }]);
  assert.equal(viewer.hasWorkspaceChanges, false);

  viewer._workspaceHandler([{
    session_name: 'auto-live', is_dirty: true, dirty_count: 2,
    commits_ahead: 1,
  }]);
  assert.equal(viewer.hasWorkspaceChanges, true);
  assert.equal(viewer.hasCommitsAhead, true);
  assert.match(viewer.workspaceStatusTooltip, /2 dirty files/);
  assert.match(viewer.workspaceStatusTooltip, /1 commit to review/);
});
