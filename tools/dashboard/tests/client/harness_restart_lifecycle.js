// Deterministic lifecycle tests for the real shared SSE client.
// Drives reordered and delayed restart frames through FakeEventSource while a
// fake clock proves there is one final dismissal and no banner resurrection.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || '/workspace/repo';
const EVENTS_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/events.js');

function makeHarness() {
  let now = 1_000_000;
  let nextTimer = 1;
  const timers = new Map();
  const documentListeners = {};
  const windowListeners = {};

  class FakeDate extends Date {
    static now() { return now; }
  }

  function schedule(fn, delay, interval) {
    const id = nextTimer++;
    timers.set(id, { fn, at: now + (delay || 0), interval: interval || 0 });
    return id;
  }
  function clear(id) { timers.delete(id); }
  function advance(ms) {
    const end = now + ms;
    while (true) {
      let selected = null;
      for (const [id, timer] of timers) {
        if (timer.at <= end && (!selected || timer.at < selected.timer.at)) {
          selected = { id, timer };
        }
      }
      if (!selected) break;
      now = selected.timer.at;
      if (selected.timer.interval) {
        selected.timer.at += selected.timer.interval;
      } else {
        timers.delete(selected.id);
      }
      selected.timer.fn();
    }
    now = end;
  }

  const stores = {};
  const Alpine = {
    store(name, value) {
      if (value !== undefined) stores[name] = value;
      return stores[name];
    },
  };
  const document = {
    visibilityState: 'visible',
    addEventListener(name, fn) {
      (documentListeners[name] ||= []).push(fn);
    },
  };
  class FakeEventSource {
    constructor() {
      this.listeners = {};
      FakeEventSource.instance = this;
    }
    addEventListener(topic, fn) { this.listeners[topic] = fn; }
    close() {}
    emit(seq, epoch, topic, data) {
      const listener = this.listeners[topic];
      if (!listener) throw new Error(`no listener for ${topic}`);
      listener({ lastEventId: `${seq}:${epoch}`, data: JSON.stringify(data) });
    }
  }
  const window = {
    Alpine, document,
    addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); },
    crypto: { randomUUID: () => 'test-client' },
    location: { pathname: '/', search: '' },
    screen: { width: 390, height: 844 },
    innerWidth: 390,
    innerHeight: 844,
  };
  const sandbox = {
    window, document, Alpine, EventSource: FakeEventSource,
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    navigator: { onLine: true }, sessionStorage: { getItem: () => null, setItem: () => {} },
    console, Promise, JSON, Object, Array, Set, Map, Error,
    parseInt, parseFloat, isFinite, Date: FakeDate,
    setTimeout: (fn, delay) => schedule(fn, delay, 0),
    clearTimeout: clear,
    setInterval: (fn, delay) => schedule(fn, delay, delay),
    clearInterval: clear,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(EVENTS_JS, 'utf8'), sandbox, { filename: 'events.js' });
  for (const fn of documentListeners['alpine:init'] || []) fn();
  window.registerHandler('nav', () => {});
  advance(0); // events.js's deferred initial connection

  return { Alpine, FakeEventSource, advance };
}

let failures = 0;
function check(condition, message) {
  if (!condition) {
    failures++;
    console.error(`FAIL: ${message}`);
  } else {
    console.log(`PASS: ${message}`);
  }
}

function beginRestart(h, startedAt = 900_000) {
  h.FakeEventSource.instance.emit(1, 10, 'nav', {}); // establish old epoch
  h.FakeEventSource.instance.emit(2, 10, 'server:restart', {
    phase: 'countdown', started_at_ms: startedAt,
    countdown_ends_at_ms: startedAt + 3000, expected_ms: 30000,
  });
}

function testCompletionConvergesAndDismissesOnce() {
  const h = makeHarness();
  beginRestart(h);
  const original = h.Alpine.store('app').restartStatus;

  h.FakeEventSource.instance.emit(1, 11, 'nav', {});
  check(h.Alpine.store('app').restartStatus === original,
    'epoch recovery preserves the restart lifecycle object');
  check(original.phase === 'complete', 'new epoch marks restart complete');

  h.advance(1000);
  h.FakeEventSource.instance.emit(2, 11, 'server:restart', {
    phase: 'complete', started_at_ms: 900_000, duration_ms: 12000,
    commit_headline: 'Lifecycle fix',
  });
  check(h.Alpine.store('app').restartStatus === original,
    'server completion enriches instead of replacing restart state');
  check(original.duration_ms === 12000, 'server timing metadata is retained');

  h.advance(1999);
  check(h.Alpine.store('app').restartStatus === original,
    'banner remains for the completion grace period');
  h.advance(1);
  check(h.Alpine.store('app').restartStatus === null,
    'one completion timer dismisses the banner');
  check(h.Alpine.store('app').sseInterrupted === false,
    'dismissal also clears the generic interruption state');
}

function testMissedCompletionAndLateReplayCannotResurrect() {
  const h = makeHarness();
  beginRestart(h, 800_000);
  h.FakeEventSource.instance.emit(1, 11, 'nav', {});
  h.advance(2000);
  check(h.Alpine.store('app').restartStatus === null,
    'epoch recovery dismisses even when server completion is missed');

  h.FakeEventSource.instance.emit(2, 11, 'server:restart', {
    phase: 'complete', started_at_ms: 800_000, duration_ms: 15000,
  });
  check(h.Alpine.store('app').restartStatus === null,
    'late completion for a dismissed restart cannot resurrect the banner');
}

function testNewRestartSupersedesOldTimer() {
  const h = makeHarness();
  beginRestart(h, 700_000);
  h.FakeEventSource.instance.emit(1, 11, 'nav', {});
  h.advance(1000);
  h.FakeEventSource.instance.emit(2, 11, 'server:restart', {
    phase: 'countdown', started_at_ms: 1_001_000,
    countdown_ends_at_ms: 1_004_000, expected_ms: 30000,
  });
  const second = h.Alpine.store('app').restartStatus;
  h.advance(1000);
  check(h.Alpine.store('app').restartStatus === second,
    'an older restart timer cannot dismiss a newer restart');
}

function testManualDismissalCannotBeReopened() {
  const h = makeHarness();
  beginRestart(h, 600_000);
  h.Alpine.store('app').dismissRestart();
  h.FakeEventSource.instance.emit(3, 10, 'server:restart', {
    phase: 'complete', started_at_ms: 600_000, duration_ms: 9000,
  });
  check(h.Alpine.store('app').restartStatus === null,
    'manual dismissal records the restart and rejects its late completion');
}

testCompletionConvergesAndDismissesOnce();
testMissedCompletionAndLateReplayCannotResurrect();
testNewRestartSupersedesOldTimer();
testManualDismissalCannotBeReopened();
process.exitCode = failures ? 1 : 0;
