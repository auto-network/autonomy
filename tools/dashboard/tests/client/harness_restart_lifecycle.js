// Deterministic lifecycle tests for the real shared SSE client's restart banner.
// Drives server:restart frames and epoch changes through FakeEventSource under a
// fake clock. The model under test (events.js):
//   restarting/countdown → banner with cause + progress, page stays live
//   complete             → "complete", one timer dismisses after 2.5 s
//   epoch change         → keeps a shown restart (10 s fallback completes it);
//                          no restart shown → brief "Server restarted", unless a
//                          restart finished in the last 2 min (never resurrect)

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

  const app = () => Alpine.store('app');
  const emit = (...a) => FakeEventSource.instance.emit(...a);
  return { app, emit, advance, now: () => now };
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

const CAUSE = { trigger: 'merge', summary: 'auto-0906-155300 · fix the banner', commit_headline: 'fix the banner' };

function beginHandoff(h, startedAt) {
  h.emit(1, 10, 'nav', {});                              // old epoch established
  h.emit(2, 10, 'server:restart', {
    phase: 'restarting', started_at_ms: startedAt, expected_ms: 30000, attribution: CAUSE,
  });
}

function testHandoffShowsCauseAndProgressWhileTheOldWorkerServes() {
  const h = makeHarness();
  beginHandoff(h, h.now());
  const st = h.app().restartStatus;
  check(st && st.phase === 'restarting', 'hand-off announcement shows the restarting banner');
  check(h.app().restartMessage().includes('auto-0906-155300 · fix the banner'),
    'banner names the session and commit that caused the reload');
  check(h.app().restartMessage().startsWith('Reloading in the background'),
    'banner says the reload is in the background');
  h.advance(15000);
  check(h.app().restartStatus === st, 'banner persists while the replacement boots');
  check(h.app().restartProgress() === 50, 'progress bar tracks elapsed/expected (15 s of 30 s)');
  check(h.app().sseInterrupted === false, 'no generic interruption banner alongside it');
}

function testEpochChangeKeepsTheBannerAndCompletionDismissesOnce() {
  const h = makeHarness();
  const t0 = h.now();
  beginHandoff(h, t0);
  const st = h.app().restartStatus;
  h.advance(20000);
  h.emit(1, 11, 'nav', {});                              // old worker gone, new one serving
  check(h.app().restartStatus === st && st.phase === 'restarting',
    'the new epoch does not end the banner: the replacement is still activating');
  check(h.app().sseInterrupted === false, 'no "Server restarted" banner during a known reload');
  h.advance(800);
  h.emit(2, 11, 'server:restart', {
    phase: 'complete', started_at_ms: t0, duration_ms: 20800, commit_headline: 'fix the banner',
  });
  check(h.app().restartStatus === st && st.phase === 'complete', 'completion converges on the same object');
  check(h.app().restartMessage().startsWith('Reload complete in 20.8s'), 'completion shows the true duration');
  check(h.app().restartProgress() === 100, 'progress bar is full on completion');
  h.advance(2499);
  check(h.app().restartStatus === st, 'banner stays for the grace period');
  h.advance(1);
  check(h.app().restartStatus === null, 'ONE timer dismisses it');
  // Nothing after this may bring it back.
  h.emit(3, 11, 'server:restart', { phase: 'complete', started_at_ms: t0, duration_ms: 20800 });
  h.emit(4, 12, 'nav', {});                              // a second epoch bump (a worker restart while quiet)
  h.advance(60000);
  check(h.app().restartStatus === null, 'late completion frames cannot resurrect the banner');
  check(h.app().sseInterrupted === false, 'an epoch change right after a finished reload shows nothing');
}

function testMissedCompletionFallsBackAfterTheNewEpoch() {
  const h = makeHarness();
  beginHandoff(h, h.now());
  const st = h.app().restartStatus;
  h.emit(1, 11, 'nav', {});
  h.advance(9999);
  check(h.app().restartStatus === st && st.phase === 'restarting', 'fallback has not fired yet');
  h.advance(1);
  check(st.phase === 'complete', 'a missed completion is synthesised 10 s after the new epoch');
  check(st.duration_ms === 10000, 'the synthesised completion reports the elapsed time, not 0.0s');
  check(h.app().restartMessage().startsWith('Reload complete in 10.0s'), 'and the message shows it');
  h.advance(2500);
  check(h.app().restartStatus === null, 'and then dismissed');
}

function testManualDismissalIsFinal() {
  const h = makeHarness();
  const t0 = h.now();
  beginHandoff(h, t0);
  h.app().dismissRestart();
  check(h.app().restartStatus === null, 'Continue dismisses immediately');
  h.emit(1, 11, 'nav', {});                              // the reconnect that used to resurrect it
  h.emit(2, 11, 'server:restart', { phase: 'complete', started_at_ms: t0, duration_ms: 9000 });
  h.advance(30000);
  check(h.app().restartStatus === null, 'neither the epoch change nor the completion reopens it');
  check(h.app().sseInterrupted === false, 'and no generic banner appears in its place');
}

function testNewerRestartSupersedesOlder() {
  const h = makeHarness();
  const t0 = h.now();
  beginHandoff(h, t0);
  h.advance(5000);
  h.emit(3, 10, 'server:restart', { phase: 'restarting', started_at_ms: t0 + 5000, expected_ms: 30000 });
  const second = h.app().restartStatus;
  check(second.started_at_ms === t0 + 5000, 'a newer announcement replaces the older banner');
  h.emit(4, 11, 'server:restart', { phase: 'complete', started_at_ms: t0, duration_ms: 5000 });
  check(h.app().restartStatus === second && second.phase === 'restarting',
    "the older restart's completion is ignored");
  h.emit(5, 11, 'server:restart', { phase: 'restarting', started_at_ms: t0, expected_ms: 30000 });
  check(h.app().restartStatus === second, 'a stale announcement for the older restart is ignored');
}

function testLegacyCountdownStillWorks() {
  const h = makeHarness();
  const t0 = h.now();
  h.emit(1, 10, 'nav', {});
  h.emit(2, 10, 'server:restart', {
    phase: 'countdown', started_at_ms: t0, countdown_ends_at_ms: t0 + 3000, expected_ms: 30000,
  });
  check(h.app().restartMessage().startsWith('Server restarting in 3'), 'countdown text');
  h.advance(3000);
  check(h.app().restartStatus.phase === 'restarting', 'countdown rolls into restarting');
  h.emit(1, 11, 'nav', {});
  h.emit(2, 11, 'server:restart', { phase: 'complete', started_at_ms: t0, duration_ms: 15000 });
  h.advance(2500);
  check(h.app().restartStatus === null, 'legacy flow completes and dismisses');
}

function testUnannouncedRestartShowsABriefNotice() {
  const h = makeHarness();
  h.emit(1, 10, 'nav', {});
  h.emit(1, 11, 'nav', {});                              // no announcement preceded this epoch
  check(h.app().sseInterrupted === 'Server restarted', 'an unannounced restart is still surfaced');
  check(h.app().restartStatus === null, 'but nothing is synthesised into a restart banner');
  h.advance(5000);
  check(h.app().sseInterrupted === false, 'and the notice clears itself');
}

function testColdTabSeesOnlyACompletionToast() {
  const h = makeHarness();
  h.emit(1, 11, 'nav', {});
  h.emit(2, 11, 'server:restart', { phase: 'complete', started_at_ms: 500_000, duration_ms: 21000 });
  check(h.app().restartStatus && h.app().restartStatus.phase === 'complete',
    'a tab that never saw the announcement gets one completion toast');
  check(h.app().restartMessage().startsWith('Reload complete in 21.0s'), 'with the server-measured duration');
  h.advance(2500);
  check(h.app().restartStatus === null, 'which dismisses on its own');
}

testHandoffShowsCauseAndProgressWhileTheOldWorkerServes();
testEpochChangeKeepsTheBannerAndCompletionDismissesOnce();
testMissedCompletionFallsBackAfterTheNewEpoch();
testManualDismissalIsFinal();
testNewerRestartSupersedesOlder();
testLegacyCountdownStillWorks();
testUnannouncedRestartShowsABriefNotice();
testColdTabSeesOnlyACompletionToast();
process.exitCode = failures ? 1 : 0;
