// The authority cell's single word/class, derived from the factor-policy
// contract's root_role + access (bead auto-gjv1h). The five approved states.
import test from 'node:test';
import assert from 'node:assert/strict';
import { authorityWord, authorityCls } from '../static/js/factor-management.js';

test('authorityWord — the five approved authority states', () => {
  assert.equal(authorityWord('individual', 'enabled'), 'Full authority');
  assert.equal(authorityWord('mfa-member', 'enabled'), 'Multi-factor w/ unlock');
  assert.equal(authorityWord('mfa-member', 'disabled'), 'Multi-factor only');
  assert.equal(authorityWord('none', 'enabled'), 'Unlock only');
  assert.equal(authorityWord('none', 'disabled'), 'No authority');
});

test('authorityCls tracks the state for styling', () => {
  assert.equal(authorityCls('individual', 'enabled'), 'au-full');
  assert.equal(authorityCls('mfa-member', 'enabled'), 'au-multi');
  assert.equal(authorityCls('mfa-member', 'disabled'), 'au-multi');
  assert.equal(authorityCls('none', 'enabled'), 'au-unlock');
  assert.equal(authorityCls('none', 'disabled'), 'au-none');
});

test('full authority never depends on sign-in (individual is always full)', () => {
  // root_role individual implies root access; access only splits the lower rungs.
  assert.equal(authorityWord('individual', 'disabled'), 'Full authority');
});
