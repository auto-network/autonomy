// L1 derivation test for window.Autonomy.lifecycle.*
//
// Tests the unified startup_state FSM. The backend tracks one column
// per session; lifecycleState() is a near-1:1 lookup of that column.

global.window = {};
require('../static/js/lib/lifecycle.js');

const L = window.Autonomy.lifecycle;

// Each fixture sets startup_state directly (or NULL for the terminal /
// existing-session case).
const FIXTURES = {
  // NULL / unset → ready (out of launching).
  ready:                    { is_live: true, startup_state: null },
  ready_with_planning:      { is_live: true, startup_state: null, harness_state: { in_planning_mode: true } },

  // Launching FSM states map 1:1.
  requesting:               { is_live: true, startup_state: 'requesting' },
  preparing_workspace:      { is_live: true, startup_state: 'preparing_workspace' },
  launching_container:      { is_live: true, startup_state: 'launching_container' },
  container_starting:       { is_live: true, startup_state: 'container_starting' },
  entrypoint_running:       { is_live: true, startup_state: 'entrypoint_running' },
  setup_running:            { is_live: true, startup_state: 'setup_running' },
  harness_starting:         { is_live: true, startup_state: 'harness_starting' },
  confirming_trust:         { is_live: true, startup_state: 'confirming_trust' },
  composer_ready:           { is_live: true, startup_state: 'composer_ready' },
  awaiting_first_response:  { is_live: true, startup_state: 'awaiting_first_response' },
  setup_failed:             { is_live: true, startup_state: 'setup_failed' },

  // Dead branch — startup_state is irrelevant.
  dead_resumable:           { is_live: false, resumable: true },
  dead_not_resumable:       { is_live: false, resumable: false },
};

const CHIP = {
  ready: '',
  ready_with_planning: 'Planning',
  requesting: 'Queued',
  preparing_workspace: 'Preparing workspace',  // augmented in phaseChip() with N/M
  launching_container: 'Starting container',
  container_starting: 'Starting container',
  entrypoint_running: 'Initializing container',
  setup_running: 'Container setup',
  harness_starting: 'Starting harness',
  confirming_trust: 'Confirming trust',
  composer_ready: 'Sending orientation',
  awaiting_first_response: 'Awaiting first reply',
  setup_failed: 'Setup failed',
  dead_resumable: 'Ended',
  dead_not_resumable: 'Ended',
};

const TONE = {
  ready: 'ready',
  ready_with_planning: '',
  requesting: '',
  preparing_workspace: '',
  launching_container: '',
  container_starting: '',
  entrypoint_running: '',
  setup_running: '',
  harness_starting: '',
  confirming_trust: 'failed',
  composer_ready: '',
  awaiting_first_response: '',
  setup_failed: 'failed',
  dead_resumable: 'dead',
  dead_not_resumable: 'dead',
};

const STARTUP_VISIBLE_TRUE = [
  'requesting', 'preparing_workspace', 'launching_container',
  'container_starting', 'entrypoint_running', 'setup_running',
  'harness_starting', 'confirming_trust', 'composer_ready',
  'awaiting_first_response', 'setup_failed',
];
const STARTUP_VISIBLE_FALSE = [
  'ready', 'ready_with_planning', 'dead_resumable', 'dead_not_resumable',
];

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('PASS', label); }
  else { fail++; console.log('FAIL', label, '—', detail || ''); }
}

// ── lifecycleState classifies each fixture to the expected state ──
for (const [name, row] of Object.entries(FIXTURES)) {
  const actual = L.lifecycleState(row);
  check(`lifecycleState(${name})`, actual === name, `got ${actual}`);
}

// ── Chip label per state ──
for (const [name, expected] of Object.entries(CHIP)) {
  const actual = L.phaseChip(FIXTURES[name]);
  check(`phaseChip(${name}) = ${JSON.stringify(expected)}`,
        actual === expected, `got ${JSON.stringify(actual)}`);
}

// ── preparing_workspace augments with N/M when phase_progress is set ──
const wsProg = L.phaseChip({
  is_live: true, startup_state: 'preparing_workspace',
  phase_progress: { repo_index: 2, total: 3, current_repo: 'enterprise_ng' },
});
check('phaseChip(preparing_workspace, progress=2/3) = "Preparing workspace 2/3"',
      wsProg === 'Preparing workspace 2/3', `got ${JSON.stringify(wsProg)}`);

// ── Tone per state ──
for (const [name, expected] of Object.entries(TONE)) {
  const actual = L.phaseTone(FIXTURES[name]);
  check(`phaseTone(${name}) = ${JSON.stringify(expected)}`,
        actual === expected, `got ${JSON.stringify(actual)}`);
}

// ── startupVisible ──
for (const name of STARTUP_VISIBLE_TRUE) {
  check(`startupVisible(${name}) = true`,
        L.startupVisible(FIXTURES[name]) === true);
}
for (const name of STARTUP_VISIBLE_FALSE) {
  check(`startupVisible(${name}) = false`,
        L.startupVisible(FIXTURES[name]) === false);
}

// ── inlineAction (Retry/Resume) ──
check('inlineAction(setup_failed) kind=retry',
      L.inlineAction(FIXTURES.setup_failed).kind === 'retry');
check('inlineAction(dead_resumable) kind=resume',
      L.inlineAction(FIXTURES.dead_resumable).kind === 'resume');
check('inlineAction(ready) = null',
      L.inlineAction(FIXTURES.ready) === null);
check('inlineAction(dead_not_resumable) = null',
      L.inlineAction(FIXTURES.dead_not_resumable) === null);

// ── messageTone ──
check('messageTone(setup_failed) = sc-failed-message',
      L.messageTone(FIXTURES.setup_failed) === 'sc-failed-message');
check('messageTone(harness_starting) = sc-startup-message',
      L.messageTone(FIXTURES.harness_starting) === 'sc-startup-message');
check('messageTone(ready) = "" (no tone)',
      L.messageTone(FIXTURES.ready) === '');
check('messageTone(dead_resumable) = "" (no tone)',
      L.messageTone(FIXTURES.dead_resumable) === '');

// ── NULL startup_state defaults to ready (corner case folds into normal path) ──
check('startup_state undefined → ready',
      L.lifecycleState({ is_live: true }) === 'ready');
check('startup_state empty-string → ready (falsy)',
      L.lifecycleState({ is_live: true, startup_state: '' }) === 'ready');

// ── Planning overlay only applies when startup_state is NULL ──
check('startup_state=preparing_workspace + in_planning_mode → preparing_workspace (overlay suppressed during launch)',
      L.lifecycleState({
        is_live: true, startup_state: 'preparing_workspace',
        harness_state: { in_planning_mode: true },
      }) === 'preparing_workspace');

console.log('---');
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
