/* Session-control refusals as sentences, for the machine chooser, remote
 * cards and the session viewer. Loaded before the page scripts. */
(function(window) {
  // A session-control refusal as a sentence (graph://7eb29bc8-31a §9.2). The
  // code names the check that failed; ``at`` says which machine decided it
  // ('local' = this dashboard's machine, 'peer' = the target). Handshake codes
  // are stated from the machine running the check: own-* is its own material,
  // peer-* the other machine's hello. The text claims nothing the code does
  // not, and always ends with the code itself.
  var _REFUSAL_HANDSHAKE = {
    'not-in-roster': '{owner} is not an active machine in {checker}\'s fleet roster',
    'scope-undelegated': '{owner} presented no runtime delegation',
    'scope-missing': '{owner}\'s runtime delegation does not include session:control',
    'scope-excess': '{owner}\'s runtime delegation carries a scope outside fleet:sync and session:control',
    'delegation-required': '{owner} presented no runtime delegation where one is required',
    'delegation-expired': '{owner}\'s runtime delegation has expired',
    'delegation-not-yet-valid': '{owner}\'s runtime delegation is not valid yet',
    'delegation-revoked': '{owner}\'s runtime key is revoked',
    'delegation-bad-signature': '{owner}\'s runtime delegation signature does not verify',
    'delegation-wrong-org': '{owner}\'s runtime delegation names another identity',
    'delegation-malformed': '{owner}\'s runtime delegation is malformed',
    'delegation-invalid': '{owner}\'s runtime delegation does not verify',
    'delegation-not-machine-direct': '{owner}\'s runtime delegation is not machine-direct',
    'delegation-ttl-exceeded': '{owner}\'s runtime delegation exceeds its lifetime bound',
    'delegation-target-types': '{owner}\'s runtime delegation carries target types',
    'delegation-wrong-machine': '{owner}\'s runtime delegation names another machine',
    'signer-not-roster-key': '{owner} signs with a key its roster entry does not name',
    'key-not-delegated': '{owner}\'s process key does not match its delegation',
    'hello-malformed': '{owner} sent a malformed hello',
    'client-proof-failed': '{owner}\'s hello signature does not verify',
    'server-proof-failed': '{owner}\'s hello signature does not verify',
    'server-wrong-client': '{owner}\'s hello names another machine as its client',
    'server-unexpected-machine': '{owner} answered under an unexpected machine key',
  };
  var _REFUSAL_TEXT = {
    'session-control-not-granted': '{decider}\'s runtime delegation does not include session:control',
    'session-control-unarmed': '{decider} has no fleet runtime armed',
    'session-control-not-negotiated': '{here}\'s relay tunnel did not negotiate session control',
    'peer-closed-at-open': '{there} declined the connection without a reason',
    'peer-closed-in-handshake': '{there} closed the connection during the handshake',
    'peer-not-in-roster': '{there} is not in {here}\'s active fleet roster',
    'destination-slot-absent': 'the relay lists no connection for {there}',
    'slot-lookup-failed': '{here} could not read the relay\'s connection list',
    'handshake-timeout': '{there} did not complete the handshake in time',
    'reply-timeout': '{there} did not reply in time',
    'connector-call-failed': '{here}\'s dashboard could not reach its connector',
    'connector-refused-request': '{here}\'s connector did not run the request',
    'unknown-machine': '{there} is not an active machine of this fleet',
    'peer-refused': '{there} refused with a reason this machine does not recognise',
  };
  window.describeRefusal = function(ref, names) {
    ref = ref || {};
    names = names || {};
    var cap = function(v) { v = String(v || ''); return v.charAt(0).toUpperCase() + v.slice(1); };
    var here = cap(names.here || 'this machine');
    var there = cap(names.there || 'the other machine');
    var code = String(ref.reason || ref.refusal || '');
    var decider = ref.at === 'peer' ? there : here;
    var other = ref.at === 'peer' ? here : there;
    var text = null;
    var m = /^(own|peer)-(.+)$/.exec(code);
    if (m && _REFUSAL_HANDSHAKE[m[2]]) {
      text = _REFUSAL_HANDSHAKE[m[2]]
        .replace('{owner}', m[1] === 'own' ? decider : other)
        .replace('{checker}', decider);
    } else if (_REFUSAL_TEXT[code]) {
      text = _REFUSAL_TEXT[code];
    }
    if (!text) text = 'remote sessions refused';
    text = text.replace(/\{decider\}/g, decider).replace(/\{here\}/g, here)
               .replace(/\{there\}/g, there);
    var by = ref.at === 'peer' ? ' (refused by ' + there + ')' : '';
    return text.charAt(0).toUpperCase() + text.slice(1) + by + (code ? ' [' + code + ']' : '');
  };
})(typeof window !== 'undefined' ? window : globalThis);
