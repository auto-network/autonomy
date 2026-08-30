/**
 * Shared harness: evaluate static/js/pages/sessions.js in a bare vm sandbox
 * and hand back the Alpine sessionsPage component for direct unit testing.
 */
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
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    removeEventListener() {},
    dispatchEvent(event) {
      (listeners[event && event.type] || []).forEach((callback) => callback(event));
      return true;
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

module.exports = { REPO_ROOT, SESSIONS_JS, SESSIONS_HTML, makeSessionsPage };
