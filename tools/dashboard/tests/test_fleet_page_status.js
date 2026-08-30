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

const authorized = (extra) => Object.assign(
  { standing: 'authorized', rowKind: 'machine', presence: 'connected' }, extra);

describe('fleet card status derivation', () => {
  it('a machine failing every sync with no pull ever completed is not healthy', () => {
    const page = boot();
    // The observed trial state: 0 completed, 24 failed, never a success.
    const machine = authorized({
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24,
    });
    assert.equal(page.syncTrouble(machine), true);
    assert.equal(page.dotClass(machine), 'warn', 'dot leaves neutral/connected');
    assert.equal(page.statusTone(machine), 'warn', 'status column is toned');
    assert.equal(page.machineClass(machine), 'warn', 'card border is toned');
    assert.notEqual(page.dotClass(machine), 'connected');
    assert.notEqual(page.dotClass(machine), '');
  });

  it('a machine with no attempts at all is quiet, never marked failed', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 0,
    });
    assert.equal(page.syncTrouble(machine), false);
    // Not failed/warn; its presence still drives the dot (here: connected).
    assert.equal(page.machineClass(machine), '');
    assert.equal(page.statusTone(machine), '');
    assert.equal(page.dotClass(machine), 'connected');
  });

  it('a machine mid-first-sync (trying, no failures yet) is not marked failed', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 0,
      syncIterations: 1, presence: '',
    });
    assert.equal(page.syncTrouble(machine), false);
    assert.equal(page.dotClass(machine), '');
    assert.equal(page.machineClass(machine), '');
  });

  it('a machine with a recent successful pull renders healthy', () => {
    const page = boot();
    const machine = authorized({
      lastSuccessfulSyncAt: Date.now() - 30000,
      successfulIterations: 12, failedIterations: 1,
    });
    assert.equal(page.syncTrouble(machine), false);
    assert.equal(page.dotClass(machine), 'connected');
    assert.equal(page.statusTone(machine), '');
    assert.equal(page.machineClass(machine), '');
  });

  it('the local machine never reads as a sync failure', () => {
    const page = boot();
    const machine = authorized({
      isLocalMachine: true,
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24,
    });
    assert.equal(page.syncTrouble(machine), false);
  });

  it('a pending-admission row is never marked as failing sync', () => {
    const page = boot();
    const machine = {
      standing: 'pending_approval', rowKind: 'pending_admission',
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 5,
    };
    assert.equal(page.syncTrouble(machine), false);
    assert.equal(page.dotClass(machine), 'pending');
    assert.equal(page.machineClass(machine), 'pending');
  });

  it('a hard standing failure (revoked/admission_failed) still wins over sync tone', () => {
    const page = boot();
    const revoked = authorized({
      standing: 'revoked',
      lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24,
    });
    assert.equal(page.dotClass(revoked), 'failed');
    assert.equal(page.machineClass(revoked), 'failed');
  });

  it('the fleet summary counts machines in a failing state', () => {
    const page = boot();
    page.view = { machines: [
      authorized({ lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 24 }),
      authorized({ lastSuccessfulSyncAt: Date.now(), successfulIterations: 3, failedIterations: 0 }),
      authorized({ lastSuccessfulSyncAt: null, successfulIterations: 0, failedIterations: 0 }),
    ] };
    assert.equal(page.failingMachines, 1, 'only the failing machine is counted');
  });
});
