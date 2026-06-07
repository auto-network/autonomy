// L1 derivation test for window.Autonomy.lifecycle.*
//
// Covers the lifecycle.js state machine after the auto-ja51w redesign:
//   - 6-phase launching window (requesting / preparing_workspace /
//     launching_container / booting_harness / awaiting_first_response /
//     ready)
//   - launching-until-first-response gate: ready requires entries > 0
//   - chip + tone match the design rev 63542418 fixtures (with the two
//     deliberate deviations: ``ready`` chip suppressed, harness label
//     dynamic over s.harness)
//   - migration-artifact guard preserved for resumed sessions
//     (resolved=true + historical JSONL = entries > 0)
//   - inlineAction + messageTone unchanged from prior

global.window = {};
require('../static/js/lib/lifecycle.js');

const L = window.Autonomy.lifecycle;

// Phase-pair fixtures. ``entry_count`` is set so each fixture classifies
// to its intended state under the new launching-until-first-response gate:
//   - launching states get entry_count: 0 (no first reply yet)
//   - ready states get entry_count: 1+ (first reply emitted)
const PHASE_PAIR = {
  pending: { is_live: true, setup_phase: 'pending', harness_phase: 'pending', entry_count: 0 },
  // auto-ja51w pre-container phases (register-early pattern)
  requesting: { is_live: true, setup_phase: 'requesting', harness_phase: 'pending', entry_count: 0 },
  preparing_workspace: { is_live: true, setup_phase: 'preparing_workspace', harness_phase: 'pending', entry_count: 0 },
  launching_container: { is_live: true, setup_phase: 'launching_container', harness_phase: 'pending', entry_count: 0 },
  booting_harness: { is_live: true, setup_phase: 'container_started', harness_phase: 'pending', entry_count: 0, harness: 'claude' },
  // existing in-container phases
  container_starting: { is_live: true, setup_phase: 'container_starting', harness_phase: 'harness_starting', entry_count: 0 },
  entrypoint: { is_live: true, setup_phase: 'entrypoint_running', harness_phase: 'pending', entry_count: 0 },
  setup_running: { is_live: true, setup_phase: 'setup_running', harness_phase: 'harness_starting', entry_count: 0 },
  harness_starting: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', harness: 'claude', entry_count: 0 },
  first_turn_written: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'first_turn_written', entry_count: 0 },
  // new "harness up, waiting on first reply" state
  awaiting_first_response: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready', entry_count: 0 },
  // ready requires entry_count > 0 under the new gate
  ready: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready', entry_count: 1 },
  ready_with_confirming_trust: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready', entry_count: 1, harness_state: { confirming_trust_prompt: true } },
  ready_with_planning: { is_live: true, setup_phase: 'setup_complete', harness_phase: 'composer_ready', entry_count: 1, harness_state: { in_planning_mode: true } },
  setup_failed: { is_live: true, setup_phase: 'setup_failed', harness_phase: 'pending', entry_count: 0 },
  dead_resumable: { is_live: false, resumable: true },
  dead_not_resumable: { is_live: false, resumable: false },
};

// Chip labels — these are the strings the operator sees.
const DESIGN_CHIP = {
  pending: 'Queued',
  requesting: 'Queued',
  preparing_workspace: 'Preparing workspace',  // sans N/M when no phase_progress
  launching_container: 'Starting container',
  // booting_harness: dynamic — see "harness-dynamic" test
  container_starting: 'Starting container',
  entrypoint: 'Preparing workspace',
  setup_running: 'Setup running',
  // harness_starting: dynamic — see "harness-dynamic" test
  first_turn_written: 'Verifying input',
  awaiting_first_response: 'Awaiting first reply',
  ready: '',                                  // deliberate suppression (host-approved)
  ready_with_confirming_trust: 'Confirming trust',
  ready_with_planning: 'Planning',
  setup_failed: 'Setup failed',
  dead_resumable: 'Ended',
  dead_not_resumable: 'Ended',
};

const DESIGN_TONE = {
  pending: '',
  requesting: '',
  preparing_workspace: '',
  launching_container: '',
  booting_harness: '',
  container_starting: '',
  entrypoint: '',
  setup_running: '',
  harness_starting: '',
  first_turn_written: '',
  awaiting_first_response: '',
  ready: 'ready',                             // moot — chip is suppressed
  ready_with_confirming_trust: 'failed',
  ready_with_planning: '',
  setup_failed: 'failed',
  dead_resumable: 'dead',
  dead_not_resumable: 'dead',
};

const STARTUP_VISIBLE_TRUE = [
  'pending', 'requesting', 'preparing_workspace', 'launching_container',
  'booting_harness', 'container_starting', 'entrypoint', 'setup_running',
  'harness_starting', 'first_turn_written', 'awaiting_first_response',
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

// ── LAUNCHING-UNTIL-FIRST-RESPONSE GATE (auto-ja51w) ──
// composer_ready + entries=0 → awaiting_first_response (NOT ready)
check('composer_ready + entries=0 → awaiting_first_response',
      L.lifecycleState({is_live:true, setup_phase:'setup_complete', harness_phase:'composer_ready', entry_count:0}) === 'awaiting_first_response');
check('composer_ready + entries=1 → ready',
      L.lifecycleState({is_live:true, setup_phase:'setup_complete', harness_phase:'composer_ready', entry_count:1}) === 'ready');
check('resolved + entries=0 + no composer_ready → pending (not ready)',
      L.lifecycleState({is_live:true, resolved:true, setup_phase:'pending', harness_phase:'pending', entry_count:0}) === 'pending');
check('resolved + entries=1 → ready (resumed session with historical JSONL)',
      L.lifecycleState({is_live:true, resolved:true, setup_phase:'pending', harness_phase:'pending', entry_count:5}) === 'ready');
check('entries_length override beats entry_count',
      L.lifecycleState({is_live:true, setup_phase:'setup_complete', harness_phase:'composer_ready', entry_count:0, entries_length:1}) === 'ready');

// composer_ready + overlays still surface the overlay (even while
// awaiting first response, an agent could be at trust prompt or planning).
check('composer_ready + entries=0 + confirming_trust → ready_with_confirming_trust',
      L.lifecycleState({is_live:true, setup_phase:'setup_complete', harness_phase:'composer_ready', entry_count:0,
                        harness_state:{confirming_trust_prompt:true}}) === 'ready_with_confirming_trust');
check('composer_ready + entries=0 + planning → ready_with_planning',
      L.lifecycleState({is_live:true, setup_phase:'setup_complete', harness_phase:'composer_ready', entry_count:0,
                        harness_state:{in_planning_mode:true}}) === 'ready_with_planning');

// setup_failed wins over the launching-until-first-response gate
check('setup_failed wins over composer_ready (with entries)',
      L.lifecycleState({is_live:true, setup_phase:'setup_failed', harness_phase:'composer_ready', entry_count:5}) === 'setup_failed');
check('setup_failed wins over resolved',
      L.lifecycleState({is_live:true, resolved:true, setup_phase:'setup_failed', harness_phase:'pending', entry_count:5}) === 'setup_failed');

// Dead sessions ignore everything else
check("dead session ignores resolved + entries",
      L.lifecycleState({is_live:false, resolved:true, resumable:true, entry_count:100}) === 'dead_resumable');

// ── PRE-CONTAINER PHASES (auto-ja51w register-early pattern) ──
// These four phases fire during the ~7-9s server-side window before
// dind-entrypoint starts writing .setup_phase markers. They cover what
// was previously dead-air (only the optimistic-tile "Queued" placeholder).
check('requesting maps to requesting state',
      L.lifecycleState({is_live:true, setup_phase:'requesting', harness_phase:'pending'}) === 'requesting');
check('preparing_workspace maps to preparing_workspace state',
      L.lifecycleState({is_live:true, setup_phase:'preparing_workspace', harness_phase:'pending'}) === 'preparing_workspace');
check('launching_container maps to launching_container state',
      L.lifecycleState({is_live:true, setup_phase:'launching_container', harness_phase:'pending'}) === 'launching_container');
check('container_started maps to booting_harness state',
      L.lifecycleState({is_live:true, setup_phase:'container_started', harness_phase:'pending'}) === 'booting_harness');

// ── phaseChip matches design (static states) ──
for (const [name, expected] of Object.entries(DESIGN_CHIP)) {
  const actual = L.phaseChip(PHASE_PAIR[name]);
  check(`phaseChip(${name}) = ${JSON.stringify(expected)}`,
        actual === expected, `got ${JSON.stringify(actual)}`);
}

// ── phaseChip harness_starting / booting_harness are dynamic over s.harness ──
{
  const claude = L.phaseChip({ is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', harness: 'claude', entry_count: 0 });
  check('phaseChip(harness_starting, claude) = "Booting Claude"',
        claude === 'Booting Claude', `got ${JSON.stringify(claude)}`);
  const codex = L.phaseChip({ is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', harness: 'codex', entry_count: 0 });
  check('phaseChip(harness_starting, codex) = "Booting Codex"',
        codex === 'Booting Codex', `got ${JSON.stringify(codex)}`);
  const bhClaude = L.phaseChip({ is_live: true, setup_phase: 'container_started', harness_phase: 'pending', harness: 'claude', entry_count: 0 });
  check('phaseChip(booting_harness, claude) = "Booting Claude"',
        bhClaude === 'Booting Claude', `got ${JSON.stringify(bhClaude)}`);
  const bhCodex = L.phaseChip({ is_live: true, setup_phase: 'container_started', harness_phase: 'pending', harness: 'codex', entry_count: 0 });
  check('phaseChip(booting_harness, codex) = "Booting Codex"',
        bhCodex === 'Booting Codex', `got ${JSON.stringify(bhCodex)}`);
  const unknown = L.phaseChip({ is_live: true, setup_phase: 'setup_complete', harness_phase: 'harness_starting', entry_count: 0 });
  check('phaseChip(harness_starting, no harness field) = "Booting Harness"',
        unknown === 'Booting Harness', `got ${JSON.stringify(unknown)}`);
}

// ── phaseChip preparing_workspace augments with N/M from phase_progress ──
{
  const noProgress = L.phaseChip({ is_live: true, setup_phase: 'preparing_workspace', harness_phase: 'pending', entry_count: 0 });
  check('phaseChip(preparing_workspace, no progress) = "Preparing workspace"',
        noProgress === 'Preparing workspace', `got ${JSON.stringify(noProgress)}`);
  const withProgress = L.phaseChip({
    is_live: true, setup_phase: 'preparing_workspace', harness_phase: 'pending', entry_count: 0,
    phase_progress: { repo_index: 2, total: 3, current_repo: 'enterprise_ng' },
  });
  check('phaseChip(preparing_workspace, progress=2/3) = "Preparing workspace 2/3"',
        withProgress === 'Preparing workspace 2/3', `got ${JSON.stringify(withProgress)}`);
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
