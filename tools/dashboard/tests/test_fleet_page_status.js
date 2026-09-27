// Status-derivation proof for the Fleet page card (bead auto-9yp8r).
// Drives the REAL page.js through jsdom — not a copy of its logic — so the
// card's status dot, status-column tone and machine border must all read a
// machine's sync OUTCOMES, not just its standing/presence. The regression:
// a machine that failed every sync it ever attempted rendered as healthy
// because the derivation never looked at the sync fields.
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const PAGE_JS = path.join(__dirname, '..', 'plugins', 'fleet', 'page.js');

function boot() {
  const src = fs.readFileSync(PAGE_JS, 'utf8');
  const dom = new JSDOM('<!DOCTYPE html><body></body>', { runScripts: 'outside-only' });
  dom.window.eval(src);
  return dom.window.fleetPage();
}

// Roster rows as the projection sends them. Since the Design Studio redesign
// (5a637e5a) one classifier, machineState(), drives the dot, the status
// column and the card border; syncTrouble() is gone with the old derivation.
const authorized = (extra) => Object.assign(
  { standing: 'authorized', rowKind: 'roster_machine', presence: 'connected' }, extra);

describe('fleet card status derivation', () => {
  it('a machine failing every sync with no pull ever completed is not healthy', () => {
    const page = boot();
    // The observed trial state: 0 completed, 24 failed, never a success.
    const machine = authorized({
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24,
    });
    assert.equal(page.machineHealthy(machine), false);
    assert.equal(page.dotClass(machine), 'warn', 'dot leaves neutral/connected');
    assert.equal(page.statusTone(machine), 'warn', 'status column is toned');
    assert.equal(page.machineClass(machine), 'warn', 'card border is toned');
    assert.notEqual(page.dotClass(machine), 'connected');
  });

  it('a machine that synced before and now fails is marked failing', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: Date.now() - 60000, lastOutcome: 'failed',
      lastErrorCode: 'auth_denied',
    });
    const state = page.machineState(machine);
    assert.equal(state.id, 'failing');
    assert.equal(page.dotClass(machine), 'failed');
    assert.equal(page.machineClass(machine), 'failed');
  });

  it('a machine with no attempts at all is waiting, never marked failed', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 0,
    });
    assert.equal(page.machineState(machine).id, 'first');
    assert.notEqual(page.statusTone(machine), 'failed');
    assert.notEqual(page.dotClass(machine), 'failed');
  });

  it('a machine mid-first-sync (trying, no failures yet) is not marked failed', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 0,
      syncIterations: 1, presence: '',
    });
    assert.equal(page.machineState(machine).id, 'first');
    assert.notEqual(page.machineClass(machine), 'failed');
  });

  it('a machine with a recent successful pull renders healthy', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: Date.now() - 30000,
      successfulIterations: 12, failedIterations: 1,
    });
    assert.equal(page.machineState(machine).id, 'synced');
    assert.equal(page.dotClass(machine), 'connected');
    assert.equal(page.statusTone(machine), 'good');
    assert.equal(page.machineClass(machine), '');
  });

  it('the local machine never reads as a sync failure', () => {
    const page = boot();
    const machine = authorized({
      isLocalMachine: true,
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24,
    });
    const state = page.machineState(machine);
    assert.notEqual(state.id, 'failing');
    assert.notEqual(state.id, 'first');
  });

  it('a pending-admission row is an admission, never a machine with a sync state', () => {
    const page = boot();
    page.view = { machines: [{
      standing: 'pending_approval', rowKind: 'pending_admission',
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 5,
    }] };
    assert.equal(page.machines.length, 0);
    assert.equal(page.admissions.length, 1);
    assert.equal(page.admissionWord(page.admissions[0]), 'Awaiting approval');
  });

  it('a revoked tombstone is not a machine', () => {
    const page = boot();
    page.view = { machines: [authorized({
      standing: 'revoked',
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24,
    })] };
    assert.equal(page.machines.length, 0);
  });

  it('the fleet summary counts the machines that are not healthy', () => {
    const page = boot();
    page.view = { machines: [
      authorized({ lastSuccessfulSyncAt: Date.now() - 60000, lastSyncOutcome: 'failed',
                   lastErrorCode: 'auth_denied' }),
      authorized({ lastSuccessfulSyncAt: Date.now(), successfulIterations: 3, failedIterations: 0 }),
    ] };
    assert.equal(page.unhealthyMachines().length, 1, 'only the failing machine is counted');
    assert.equal(page.fleetTone(), 'err');
  });
});

// The false-reassuring "first" state was a lie for an approved machine whose
// invitation link is deactivated: it read "has joined... syncing starts
// automatically once it comes online" while the join could never finish
// (invite active=0). link_off names the real fault + the fix. Operator-driven
// 2026-09-05 (SJC stuck; home showed the reassuring note).
describe('fleet card: an approved machine blocked by a deactivated invite', () => {
  it('reads link_off (not first) when the invitation is awaiting reactivation', () => {
    const page = boot();
    page.view = { invitation: { status: 'awaiting_signature' } };
    const machine = authorized({ isLocalMachine: false, lastSuccessfulSyncAt: null });
    const state = page.machineState(machine);
    assert.equal(state.id, 'link_off');
    assert.equal(state.word, 'Invite link off');
    assert.match(state.note, /invitation link is not active/);
  });

  it('still reads first (bootstrapping) when the invitation is active', () => {
    const page = boot();
    page.view = { invitation: { status: 'active' } };
    const machine = authorized({ isLocalMachine: false, lastSuccessfulSyncAt: null });
    assert.equal(page.machineState(machine).id, 'first');
  });
});
