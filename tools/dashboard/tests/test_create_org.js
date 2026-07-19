const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const org = require('../static/js/create-org.js');

describe('create-org derivations', () => {
  it('slugs a display name', () => {
    assert.equal(org.deriveOrgSlug('Riverside Robotics'), 'riverside-robotics');
  });

  it('collapses punctuation runs and trims edge dashes', () => {
    assert.equal(org.deriveOrgSlug('  --Née: Café & Co.!  '), 'n-e-caf-co');
  });

  it('caps slugs at 40 characters', () => {
    assert.equal(org.deriveOrgSlug('x'.repeat(60)).length, 40);
  });

  it('yields no slug (submit stays disabled) for punctuation-only names', () => {
    assert.equal(org.deriveOrgSlug('!!! ...'), '');
  });

  it('derives the uppercased first character as the initial', () => {
    assert.equal(org.deriveOrgInitial('riverside'), 'R');
  });

  it('has no initial (mark hidden) until a name exists', () => {
    assert.equal(org.deriveOrgInitial('   '), '');
  });

  it('derives a deterministic palette color from the name', () => {
    assert.equal(org.deriveOrgColor('Riverside Robotics'),
                 org.deriveOrgColor('Riverside Robotics'));
    assert.match(org.deriveOrgColor('anything'), /^#[0-9A-F]{6}$/i);
  });

  it('derives different colors for different names (hash spread)', () => {
    const colors = new Set(['a', 'bb', 'ccc', 'dddd', 'eeeee', 'ffffff']
      .map(org.deriveOrgColor));
    assert.ok(colors.size > 1);
  });
});
