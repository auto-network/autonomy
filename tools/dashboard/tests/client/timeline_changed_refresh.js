// auto-jnm58 — Node/jsdom-style harness for the event-driven page refresh.
//
// Loads the REAL pages/timeline.js inside a stub DOM + stub Alpine, then:
//   1. asserts init() registers a `timeline:changed` handler and NO 15 s
//      setInterval (the only interval is the 60 s heartbeat),
//   2. fires a burst of `timeline:changed` frames and asserts they coalesce
//      into exactly ONE refetch after the 5 s debounce window,
//   3. asserts the 60 s heartbeat still refetches unconditionally.
//
// Usage: node timeline_changed_refresh.js   (exit 0 pass, 1 fail)

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../../..');
const TIMELINE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/timeline.js');

// ── Controllable timers ────────────────────────────────────────────
let _timerId = 0;
const _timeouts = new Map();   // id -> {cb, delay}
const _intervals = new Map();  // id -> {cb, delay}

function setTimeoutStub(cb, delay) {
  const id = ++_timerId;
  _timeouts.set(id, { cb, delay });
  return id;
}
function clearTimeoutStub(id) { _timeouts.delete(id); }
function setIntervalStub(cb, delay) {
  const id = ++_timerId;
  _intervals.set(id, { cb, delay });
  return id;
}
function clearIntervalStub(id) { _intervals.delete(id); }

function flushTimeouts() {
  const pending = [..._timeouts.entries()];
  _timeouts.clear();
  for (const [, t] of pending) t.cb();
}
function fireInterval(delay) {
  for (const [, iv] of _intervals) if (iv.delay === delay) iv.cb();
}

// ── Stub DOM / Alpine ──────────────────────────────────────────────
let _alpineInit = null;
let _componentFactory = null;
const handlers = {};   // topic -> fn

const doc = {
  addEventListener(name, cb) { if (name === 'alpine:init') _alpineInit = cb; },
};
const alpine = {
  data(name, factory) { if (name === 'timelinePage') _componentFactory = factory; },
};

let timelineFetches = 0;
function fetchStub(url) {
  if (url.indexOf('/api/timeline/stats') === 0) {
    return Promise.resolve({ json: () => Promise.resolve({}) });
  }
  if (url.indexOf('/api/timeline') === 0) {
    timelineFetches++;
    return Promise.resolve({ json: () => Promise.resolve([]) });
  }
  return Promise.resolve({ json: () => Promise.resolve({}) });
}

const sandbox = {
  window: {},
  document: doc,
  Alpine: alpine,
  fetch: fetchStub,
  console,
  setTimeout: setTimeoutStub,
  clearTimeout: clearTimeoutStub,
  setInterval: setIntervalStub,
  clearInterval: clearIntervalStub,
  Promise, JSON, Object, Array, Map, Set, Date, Error, Math,
  parseInt, parseFloat, isNaN,
};
sandbox.window.document = doc;
sandbox.window.Alpine = alpine;
sandbox.window.fetch = fetchStub;
sandbox.registerHandler = function (topic, fn) {
  (handlers[topic] || (handlers[topic] = [])).push(fn);
};
sandbox.unregisterHandler = function (topic, fn) {
  if (handlers[topic]) handlers[topic] = handlers[topic].filter((h) => h !== fn);
};
sandbox.window.registerHandler = sandbox.registerHandler;
sandbox.window.unregisterHandler = sandbox.unregisterHandler;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(TIMELINE_JS, 'utf8'), sandbox, { filename: TIMELINE_JS });

// ── Drive ──────────────────────────────────────────────────────────
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

if (typeof _alpineInit !== 'function') {
  console.error('timeline.js did not register an alpine:init callback');
  process.exit(1);
}
_alpineInit();
if (typeof _componentFactory !== 'function') {
  console.error('timeline.js did not register the timelinePage component');
  process.exit(1);
}

const c = _componentFactory();
c.init();

// init() does one immediate refresh.
check(timelineFetches === 1, `expected 1 initial refetch, got ${timelineFetches}`);
// It registers exactly one timeline:changed handler.
check((handlers['timeline:changed'] || []).length === 1,
  'expected one timeline:changed handler registered');
// The only interval is the 60 s heartbeat — no lingering 15 s poll.
check(!_intervals.size || [..._intervals.values()].every((iv) => iv.delay === 60000),
  'expected only a 60 s heartbeat interval, found: '
    + [..._intervals.values()].map((iv) => iv.delay).join(','));

// Burst of ten timeline:changed frames within the debounce window.
const fn = handlers['timeline:changed'][0];
for (let i = 0; i < 10; i++) fn({});
check(timelineFetches === 1, 'burst must NOT refetch before the debounce fires');

// Fire the debounce timer(s): the ten frames collapse into one refetch.
flushTimeouts();
check(timelineFetches === 2,
  `debounced burst must refetch exactly once (got ${timelineFetches - 1})`);

// The 60 s heartbeat is the unconditional fallback.
fireInterval(60000);
check(timelineFetches === 3, 'the 60 s heartbeat must refetch');

if (failures.length) {
  console.error('FAIL:\n  ' + failures.join('\n  '));
  process.exit(1);
}
console.log('OK — timeline.js is event-driven (timeline:changed + 60 s heartbeat)');
process.exit(0);
