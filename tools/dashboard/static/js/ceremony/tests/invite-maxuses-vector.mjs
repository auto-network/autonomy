// Build invite bodies with the browser buildInviteBody and print them, so the
// Python event validator can confirm a browser-built max_uses invite verifies
// server-side (and that invite_pub + maxUses is refused client-side too).
import { buildInviteBody } from '../invitation.js';

const f = JSON.parse(process.argv[2]);

const out = {};
out.multi = buildInviteBody({
  grantedRole: f.role,
  expiry: f.expiry,
  sponsorPub: f.sponsor,
  tokenHash: f.token_hash,
  maxUses: f.max_uses,
});
out.single = buildInviteBody({
  grantedRole: f.role,
  expiry: f.expiry,
  sponsorPub: f.sponsor,
  tokenHash: f.token_hash,
});
try {
  buildInviteBody({
    grantedRole: f.role,
    expiry: f.expiry,
    sponsorPub: f.sponsor,
    invitePub: f.sponsor,
    maxUses: f.max_uses,
  });
  out.pub_plus_maxuses_error = null;
} catch (e) {
  out.pub_plus_maxuses_error = e.message;
}
process.stdout.write(JSON.stringify(out));
