# Path: joined by invitation

The claim was submitted the moment identity existed (§6a of the primer).
This path is about what happens after.

1. **While pending:** the claim waits on an approver in the org — a human,
   possibly asleep. Track it; continue personal setup meanwhile.
2. **On admission, the estate arrives:** the org's knowledge graph and its
   workspaces appear at once. This is the payoff moment — show it: run a
   `graph search` against the org's content, list the workspaces.
3. Orient by reading, not writing: suggest the user skim the org's pinned
   or canonical notes before contributing.
4. Personal seeding (the solo path's step 2) is now optional — offer it as
   "your personal estate, whenever you like."

Done when: admission landed, the user has seen the org's content respond
to a search, and knows which workspaces they can open.

## The blurb contract (for maintainers of this flow)

The paste blurb a no-node visitor copies from the join page carries exactly
two URLs, both assembled client-side from the page's own origin: the install
primer (`<origin>/install`) and the visitor's rebuilt invite link
(`<origin>/l/<channel token>#t=<bearer>`). The channel token reaches the
page as the `channel_token` query parameter (registry-visible transport
credential, same trust class as the `/l/` path it arrived on); the bearer
never leaves the URL fragment and is never sent to any server by that page.
The primer's join-first tracking (§6a) consumes the invite link exactly as
rebuilt — one source for the blurb text (the join page), one consumer
contract (this document).

## Converting the invite URL to an invitation code (agent instruction, literal)

The join mechanisms (`AUTONOMY_INVITE`, the loopback join API) accept a
**v2 invitation code**, not the URL. Do not paste the URL where a code is
expected. From an invite link `https://<host>/l/<TOKEN>#t=<BEARER>`, run
(from the cloned checkout):

```bash
curl -s https://<host>/v1/links/<TOKEN>/envelope
# → JSON carrying org, root_pub, invite_ref (public context; no secrets)

python3 - <<'PY'
from tools.network.invitation import invitation_from_join_url, encode_invitation
invitation = invitation_from_join_url(
    org="<org from envelope>",
    root_pub="<root_pub from envelope>",
    invite_ref="<invite_ref from envelope>",
    join_url="https://<host>/l/<TOKEN>#t=<BEARER>",  # full URL incl. fragment
)
print(encode_invitation(invitation))
PY
```

The printed code (base64 body + checksum) is what you pass as
`AUTONOMY_INVITE` at install, or in `{"invite": "<code>"}` to an
already-running node's loopback join API. The `#t=` fragment is the ledger
bearer: it goes into this conversion and nowhere else — never into a log,
a chat message beyond the user's own paste, or any other server request.
