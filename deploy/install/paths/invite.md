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
