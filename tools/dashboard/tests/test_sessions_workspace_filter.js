/**
 * Sessions launch-menu workspace filtering.
 *
 * Run: node --test tools/dashboard/tests/test_sessions_workspace_filter.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const SESSIONS_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/sessions.js');
const SESSIONS_HTML = path.join(REPO_ROOT, 'tools/dashboard/templates/pages/sessions.html');

function makeLocalStorage() {
  const values = {};
  return {
    getItem(key) { return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null; },
    setItem(key, value) { values[key] = String(value); },
  };
}

function makeSessionsPage() {
  const listeners = {};
  const components = {};
  const localStorage = makeLocalStorage();
  const document = {
    body: { classList: { add() {}, remove() {} } },
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
  };
  const Alpine = {
    data(name, factory) { components[name] = factory; },
    store() {},
  };
  const window = {
    SessionStats: {
      turnsStr() {}, ctxStr() {}, idleStr() {}, ctxWarn() {}, recencyColor() {},
    },
  };
  const sandbox = {
    window, document, Alpine, localStorage,
    console, fetch() {}, setTimeout, clearTimeout, setInterval, clearInterval,
    Date, Math, Object, Array, JSON, URLSearchParams,
  };
  window.localStorage = localStorage;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SESSIONS_JS, 'utf8'), sandbox, { filename: SESSIONS_JS });
  (listeners['alpine:init'] || []).forEach((callback) => callback());
  return components.sessionsPage();
}

describe('sessions workspace launch menu', () => {
  it('shows every organization when All Orgs is selected', () => {
    const page = makeSessionsPage();
    page.orgGroups = [
      { org: { slug: 'autonomy' }, projects: [{ id: 'autonomy' }] },
      { org: { slug: 'anchore' }, projects: [{ id: 'enterprise-ng' }] },
    ];
    page.selectedOrg = '';

    assert.deepEqual(
      Array.from(page.visibleOrgGroups, (group) => group.org.slug),
      ['autonomy', 'anchore'],
    );
  });

  it('shows only workspaces belonging to the selected organization', () => {
    const page = makeSessionsPage();
    page.orgGroups = [
      { org: { slug: 'autonomy' }, projects: [{ id: 'autonomy' }] },
      { org: { slug: 'anchore' }, projects: [{ id: 'enterprise-v5' }, { id: 'enterprise-ng' }] },
    ];
    page.selectedOrg = 'anchore';

    assert.deepEqual(
      Array.from(page.visibleOrgGroups, (group) => group.org.slug),
      ['anchore'],
    );
    assert.deepEqual(
      Array.from(page.visibleOrgGroups[0].projects, (project) => project.id),
      ['enterprise-v5', 'enterprise-ng'],
    );
  });

  it('keeps Host Terminal independent from the filtered workspace loop', () => {
    const html = fs.readFileSync(SESSIONS_HTML, 'utf8');
    assert.match(html, /x-for="group in visibleOrgGroups"/);
    assert.match(html, /data-testid="host-terminal-option"/);
    assert.ok(
      html.indexOf('data-testid="host-terminal-option"') > html.indexOf('x-for="group in visibleOrgGroups"'),
      'Host Terminal should remain outside and after the filtered workspace groups',
    );
  });
});
