// Build the org:join registry payload the browser SIGNS, exactly as
// worktrees.js does: buildOrgJoinGrantPayload({orgUuid: staged.org, ...}) plus a
// verbatim meta copy when present. The Python test compares this to the staged
// registry request — that equality is what the publish executor enforces, so a
// UUID split (genesis != binding) that once desynced them is caught here.
import { buildOrgJoinGrantPayload } from '../invitation.js';

const staged = JSON.parse(process.argv[2]);
const payload = buildOrgJoinGrantPayload({
  orgUuid: staged.org,
  inviteId: staged.invite_ref,
  inviteExpiry: staged.expires_at,
});
if (staged.meta && Object.keys(staged.meta).length) {
  payload.meta = JSON.parse(JSON.stringify(staged.meta));
}
process.stdout.write(JSON.stringify(payload));
