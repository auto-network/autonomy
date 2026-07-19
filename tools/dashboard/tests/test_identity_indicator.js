const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const indicator = require('../static/js/identity-indicator.js');

function status(overrides) {
  return Object.assign({
    personal_identity: { display_name: 'Alex' },
    passkeys: [{ credential_id: 'one' }],
    onboarding_needed: false,
    signed_in: true,
    method: 'passkey',
    enforced: true,
    gate_disabled: false,
  }, overrides || {});
}

describe('identity indicator state matrix', () => {
  it('renders loading before the status contract arrives', () => {
    assert.equal(indicator.deriveIdentityState(null), 'loading');
  });

  it('renders an explicit retry state when initial status loading fails', () => {
    assert.equal(indicator.deriveIdentityState({ error: 'offline' }), 'error');
  });

  it('routes a missing personal identity to Get started', () => {
    assert.equal(indicator.deriveIdentityState(status({
      personal_identity: null, passkeys: [], signed_in: false, enforced: false,
    })), 'bootstrap');
  });

  it('renders a returning enforced session as locked', () => {
    assert.equal(indicator.deriveIdentityState(status({ signed_in: false })), 'locked');
  });

  it('never claims authentication when access is open without a session', () => {
    const value = status({ signed_in: false, enforced: false });
    assert.equal(indicator.deriveIdentityState(value), 'open');
    assert.equal(indicator.statusLabel(value), 'Access open \u00b7 Not authenticated');
  });

  it('gives the kill switch precedence over session and enrollment state', () => {
    const value = status({ gate_disabled: true, signed_in: true });
    assert.equal(indicator.deriveIdentityState(value), 'gate-off');
    assert.equal(indicator.statusLabel(value),
      'Access open \u00b7 Auth disabled');
  });

  it('marks an unlocked identity with no passkeys as setup unfinished', () => {
    const value = status({ passkeys: [], onboarding_needed: true, method: 'password' });
    assert.equal(indicator.deriveIdentityState(value), 'setup');
    assert.equal(indicator.statusLabel(value), 'Unlocked \u00b7 Password');
  });

  it('describes a bootstrap session as unfinished setup, not a login mode', () => {
    const value = status({ passkeys: [], method: 'bootstrap' });
    assert.equal(indicator.methodLabel(value), 'Setup unfinished');
    assert.equal(indicator.statusLabel(value),
      'Unlocked \u00b7 Setup unfinished');
  });

  it('marks any unlocked identity with an enrolled passkey as ready', () => {
    assert.equal(indicator.deriveIdentityState(status()), 'ready');
  });

  it('keeps password sessions distinct without treating them as incomplete', () => {
    const value = status({ method: 'password' });
    assert.equal(indicator.deriveIdentityState(value), 'ready');
    assert.equal(indicator.statusLabel(value), 'Unlocked \u00b7 Password');
  });

  it('uses a stable fallback for incomplete identity metadata', () => {
    const value = status({ personal_identity: { display_name: '' } });
    assert.equal(indicator.identityName(value), 'Your identity');
    assert.equal(indicator.identityInitial(value), '?');
  });
});
