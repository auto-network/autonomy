/**
 * Welcome step 3 when the sign-in scan finds nothing usable.
 *
 * Windows walkthrough, 2026-09-26: the page said "Sign in to Claude, Codex or
 * Grok here, or run graph claude install", but nothing on the page signs in
 * and the command does not run inside a Compose node. The message now names
 * the folder that was scanned (the host path, not the container's
 * /host-home) and lists each harness's reason from the scan report.
 *
 * Run: node --test tools/dashboard/tests/test_welcome_no_sign_in.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');

const { noSignInMessage, noSignInDetails } = require(
  path.join(__dirname, '..', 'static', 'js', 'pages', 'welcome.js'));

describe('no sign-in found', () => {
  it('names the scanned home and gives an action that works', () => {
    const msg = noSignInMessage({ home: '/home/tester' });
    assert.match(msg, /in \/home\/tester\./);
    assert.match(msg, /Go to your workspace again/);
    assert.doesNotMatch(msg, /graph claude install/);
  });

  it('falls back to "this machine" when the node reports no home', () => {
    assert.match(noSignInMessage({}), /on this machine\./);
  });

  it('lists each harness with the host path, not the container path', () => {
    const lines = noSignInDetails({
      home: '/home/tester',
      harnesses: [
        { harness: 'claude', status: 'needs_sign_in', detail: 'no /host-home/.claude/.credentials.json' },
        { harness: 'grok', status: 'needs_sign_in', detail: '' },
      ],
    });
    assert.deepEqual(lines, [
      'Claude: no /home/tester/.claude/.credentials.json',
      'Grok: needs_sign_in',
    ]);
  });
});
