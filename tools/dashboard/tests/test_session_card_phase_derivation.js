// auto-yfcoc: pin the design's exact phase chip labels + tones + lane
// captions against the derivation in lib/lifecycle.js.
//
// Run: ``node tools/dashboard/tests/test_session_card_phase_derivation.js``
//
// Catches accidental drift from design rev
// 63542418-40f2-40cd-972b-7ae1a50f41a3 — if anyone (including me)
// edits a chip label or tone without thinking, this test fails noisily.
//
// Two intentional deviations from the design fixture, both already
// documented in lib/lifecycle.js and the auto-yfcoc commit thread:
//   1. ``ready`` state suppresses its chip (returns "") rather than
//      rendering a persistent green "Ready" pill. Approved by
//      host-0531-020038 turn 71 as an explicit deviation.
//   2. ``harness_starting`` label is dynamic over s.harness so Codex
//      sessions render "Booting Codex" instead of the hardcoded
//      "Booting Claude" the design carries. Also host-approved.

global.window = {};
require('../static/js/lib/lifecycle.js');

const L = window.Autonomy.lifecycle;

const PHASE_PAIR = {
  pending: { is_live: true, setup_phase: 'pending', harness_phase: 'pending' },
  container_starting: { is_live: true, setup_phase: 'container_starting', harness_phase: 'harness_starting' },
  entrypoint: { is_live: true, setup_phase: 'entrypoint_running', harness_phase: 'pending' },
  dind_ready: { is_live: true, setup_phase: 'dind_ready', harness_phase: 'pending' },
  setup_running: { is_live: true, setup_phase: 'setup_running', harness_phase: 'harness_starting' },
  harness_starting: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', harness: 'claude' },
  first_turn_written: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'first_turn_written' },
  ready: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready' },
  ready_with_confirming_trust: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready', harness_state: { confirming_trust_prompt: true } },
  ready_with_planning: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready', harness_state: { in_planning_mode: true } },
  setup_failed: { is_live: true, setup_phase: 'setup_failed', harness_phase: 'pending' },
  dead_resumable: { is_live: false, resumable: true },
  dead_not_resumable: { is_live: false, resumable: false },
};

// Exact strings from the design's fixture payload (see
// curl -sk https://localhost:8080/api/design/63542418-40f2-40cd-972b-7ae1a50f41a3/full)
// — bumping any of these in lifecycle.js without updating both this
// test AND the design is a drift that the host already caught once.
const DESIGN_CHIP = {
  pending: 'Queued',
  container_starting: 'Starting container',
  entrypoint: 'Preparing workspace',
  dind_ready: 'Docker ready',
  setup_running: 'Setup running',
  // harness_starting: dynamic — see "harness_starting harness-dynamic" test
  first_turn_written: 'Verifying input',
  ready: '',                                  // deliberate suppression (host-approved)
  ready_with_confirming_trust: 'Confirming trust',
  ready_with_planning: 'Planning',
  setup_failed: 'Setup failed',
  dead_resumable: 'Ended',
  dead_not_resumable: 'Ended',
};

const DESIGN_TONE = {
  pending: '',
  container_starting: '',
  entrypoint: '',
  dind_ready: '',
  setup_running: '',
  harness_starting: '',
  first_turn_written: '',
  ready: 'ready',                             // moot — chip is suppressed
  ready_with_confirming_trust: 'failed',
  ready_with_planning: '',
  setup_failed: 'failed',
  dead_resumable: 'dead',
  dead_not_resumable: 'dead',
};

const DESIGN_LANE_VALUES = {
  pending:             ['allocating', 'waiting',    'pending', 'queued'],
  container_starting:  ['created',    'container',  'pending', 'queued'],
  entrypoint:          ['created',    'entrypoint', 'pending', 'queued'],
  dind_ready:          ['created',    'docker',     'pending', 'queued'],
  setup_running:       ['created',    'running',    'booting', 'queued'],
  harness_starting:    ['created',    'complete',   'booting', 'queued'],
  first_turn_written:  ['created',    'complete',   'jsonl',   'checking'],
  setup_failed:        ['created',    'failed',     'blocked', 'held'],
};

const STARTUP_VISIBLE_TRUE = [
  'pending', 'container_starting', 'entrypoint', 'dind_ready',
  'setup_running', 'harness_starting', 'first_turn_written',
  'setup_failed',
];
const STARTUP_VISIBLE_FALSE = [
  'ready', 'ready_with_confirming_trust', 'ready_with_planning',
  'dead_resumable', 'dead_not_resumable',
];

let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) { pass++; console.log('PASS', label); }
  else { fail++; console.log('FAIL', label, '—', detail || ''); }
}

// ── lifecycleState classifies each input to the expected state ──
for (const [name, row] of Object.entries(PHASE_PAIR)) {
  const actual = L.lifecycleState(row);
  check(`lifecycleState(${name})`, actual === name, `got ${actual}`);
}

// ── phaseChip matches design (static states) ──
for (const [name, expected] of Object.entries(DESIGN_CHIP)) {
  const actual = L.phaseChip(PHASE_PAIR[name]);
  check(`phaseChip(${name}) = ${JSON.stringify(expected)}`,
        actual === expected, `got ${JSON.stringify(actual)}`);
}

// ── phaseChip harness_starting is dynamic over s.harness ──
{
  const claude = L.phaseChip({ is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', harness: 'claude' });
  check('phaseChip(harness_starting, claude) = "Booting Claude"',
        claude === 'Booting Claude', `got ${JSON.stringify(claude)}`);
  const codex = L.phaseChip({ is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', harness: 'codex' });
  check('phaseChip(harness_starting, codex) = "Booting Codex"',
        codex === 'Booting Codex', `got ${JSON.stringify(codex)}`);
  const unknown = L.phaseChip({ is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting' });
  check('phaseChip(harness_starting, no harness field) = "Booting Harness"',
        unknown === 'Booting Harness', `got ${JSON.stringify(unknown)}`);
}

// ── phaseTone matches design ──
for (const [name, expected] of Object.entries(DESIGN_TONE)) {
  const actual = L.phaseTone(PHASE_PAIR[name]);
  check(`phaseTone(${name}) = ${JSON.stringify(expected)}`,
        actual === expected, `got ${JSON.stringify(actual)}`);
}

// ── startupVisible covers the right states ──
for (const name of STARTUP_VISIBLE_TRUE) {
  check(`startupVisible(${name}) = true`,
        L.startupVisible(PHASE_PAIR[name]) === true);
}
for (const name of STARTUP_VISIBLE_FALSE) {
  check(`startupVisible(${name}) = false`,
        L.startupVisible(PHASE_PAIR[name]) === false);
}

// ── lifecycleLanes — exact label set + per-state values ──
const EXPECTED_LABELS = ['request', 'setup', 'harness', 'input'];
for (const [name, expected_values] of Object.entries(DESIGN_LANE_VALUES)) {
  const lanes = L.lifecycleLanes(PHASE_PAIR[name]);
  const labels = lanes.map((l) => l.label);
  check(`lifecycleLanes(${name}).labels = ${JSON.stringify(EXPECTED_LABELS)}`,
        JSON.stringify(labels) === JSON.stringify(EXPECTED_LABELS),
        `got ${JSON.stringify(labels)}`);
  const values = lanes.map((l) => l.value);
  check(`lifecycleLanes(${name}).values = ${JSON.stringify(expected_values)}`,
        JSON.stringify(values) === JSON.stringify(expected_values),
        `got ${JSON.stringify(values)}`);
}

// ── inlineAction — Retry on setup_failed, Resume on dead_resumable, nothing else ──
check('inlineAction(setup_failed) kind = retry',
      L.inlineAction(PHASE_PAIR.setup_failed).kind === 'retry');
check('inlineAction(setup_failed) label = "Retry"',
      L.inlineAction(PHASE_PAIR.setup_failed).label === 'Retry');
check('inlineAction(dead_resumable) kind = resume',
      L.inlineAction(PHASE_PAIR.dead_resumable).kind === 'resume');
check('inlineAction(dead_resumable) label = "Resume"',
      L.inlineAction(PHASE_PAIR.dead_resumable).label === 'Resume');
check('inlineAction(ready) = null',
      L.inlineAction(PHASE_PAIR.ready) === null);
check('inlineAction(dead_not_resumable) = null',
      L.inlineAction(PHASE_PAIR.dead_not_resumable) === null);

// ── messageTone follows phase ──
check('messageTone(setup_failed) = "sc-failed-message"',
      L.messageTone(PHASE_PAIR.setup_failed) === 'sc-failed-message');
check('messageTone(container_starting) = "sc-startup-message"',
      L.messageTone(PHASE_PAIR.container_starting) === 'sc-startup-message');
check('messageTone(ready) = "" (no tone)',
      L.messageTone(PHASE_PAIR.ready) === '');
check('messageTone(dead_resumable) = "" (no tone)',
      L.messageTone(PHASE_PAIR.dead_resumable) === '');

console.log('---');
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
