# Install Autonomy

You are probably a coding agent whose user said something like *"install
Autonomy from auto.network/install."* This document is written for you. It
tells you what Autonomy is, how to install and verify it, how to onboard
your user well, and how to check every claim it makes on the machine in
front of you. A human reading this directly: welcome — every command works
by hand too, and the browser version of this page has a copy button for
handing it to your agent.

Canonical source: `deploy/install/INSTALL.md` in the Autonomy repository.
The copy served at `auto.network/install` is convenience distribution, not
a control point — after you clone, prefer your local copy, and diff it
against the served one if you care to.

---

## 0. Your contract as the installing agent

Follow these regardless of which path you take below:

1. **Consent before mutation.** After preflight (read-only), brief your
   user on exactly what will change and ask before proceeding. A template
   is in §3. Re-ask at each later privilege step (installing Docker itself,
   installing a skill into your own harness).
2. **You never touch identity secrets.** Autonomy identities are created in
   the user's browser; the passphrase, recovery code, and root key never
   pass through you. When the flow reaches identity, hand the human to
   their browser and wait. No exceptions; no flow in this document asks you
   to handle them.
3. **Verify, don't relay.** Every marketing-shaped claim in this document
   has a command you can run locally to check it — see
   [/install/verify.md](/install/verify.md). Show your user the receipts,
   not our sentences.
4. **Track the work.** Load §1's task list into your native task tracker
   before starting. This install includes waits and human hand-offs — the
   places untracked agents drop threads.
5. **Report honestly.** What's installed, what's pending, what failed. If a
   step fails, capture the error before retrying.

## 1. The task list

Load these as tracked tasks now. Stage tags matter: OUTER tasks run here in
your harness; INNER tasks are best done from a workspace session inside
Autonomy once it's running (§7) — if that's not available, you do them
yourself, slower, from here.

```
[outer] 1. Preflight: observe the machine (§2)
[outer] 2. Brief the user; get consent (§3)
[outer] 3. Install and start the node (§4)
[outer] 4. Verify the claims; show receipts (§5, /install/verify.md)
[outer] 5. Identity hand-off — human + browser only (§6)   [BLOCKED ON HUMAN]
[outer] 6. If an invitation is in hand: submit the claim NOW (§6a)
[outer] 7. Connect the first workspace; provision session credentials (§7)
[outer] 8. Launch the onboarding session; pass the baton (§7)
[inner] 9. Seed the graph from the user's own work (propose, never act) (§8)
[inner] 10. Build out the chosen garden path (§8)
[inner] 11. Write the "how I set this up" note into the new graph (§9)
[outer] 12. Install the autonomy skill into this harness; recede (§9)
```

## 2. What Autonomy is (and preflight)

**Autonomy is software a person owns.** A self-hosted node — dashboard,
knowledge graph, agent-session runner — where no central server holds
accounts or data. Identity is a cryptographic key the user holds, never an
account that can be suspended. Organizations are tamper-evident ledgers of
cryptographic membership. Sharing happens through opaque links the user
mints and can revoke. Agents (like you) get a persistent substrate: a graph
that remembers across sessions, a work ledger, and a way to run many
sessions against the user's own repositories.

You already know why that matters, because the reader of this document
loses its memory at the end of every conversation. The graph is the memory
that persists. That's the honest pitch, and §5 is how you check the rest.

**Preflight — observe before asking** (all read-only):

- Platform and resources: Linux, macOS, or WSL2; Docker present
  (`docker --version`, `docker compose version`); a few GB of disk.
- **Already installed?** Probe `https://localhost:8080` (accept a
  self-signed cert). A responding Autonomy dashboard means your job is
  handoff, not installation — tell the user. Do not send an invitation or a
  personal password to a loopback API: interactive invitation acceptance is
  a browser-local identity ceremony, not a server-side password handoff.
- Context you already have: the user's repositories, their git identity,
  whether teammates appear in commit history, whether an invitation link is
  in hand. Prefer inference over questions; aim to ask at most three
  (which path in §8, the org name, seed-or-blank).

If Docker is absent, that's its own consent gate: propose installing it via
the platform's standard channel, or stop and report. Platform notes:
[/install/platform/wsl2.md](/install/platform/wsl2.md) ·
[/install/platform/mac.md](/install/platform/mac.md) ·
[/install/platform/linux.md](/install/platform/linux.md).

## 3. The consent briefing

Deliver a briefing in your own words with the observed specifics filled in.
Template:

> I'll set up your system to run Autonomy — a sovereign, self-hosted
> platform you own: your identity is a key you hold, your data stays in one
> Docker volume on this machine, and nothing phones home. Concretely I
> will: clone the source to `<path>`, build (or pull and verify) the node
> image (~2 GB), and start one container serving `https://localhost:8080`.
> It creates no account and sends no telemetry — I'll verify both claims on
> this machine and show you. Shall I proceed?

Nothing that mutates the system runs before an explicit yes.

## 4. Install and start the node

**Path A — build from source you chose (the default sovereign path):**

```bash
git clone <source you chose> autonomy && cd autonomy
AUTONOMY_FIRST_ORG=myorg docker compose up -d
# → https://localhost:8080  (self-signed cert; accept once)
```

`AUTONOMY_FIRST_ORG` founds a new organization (pick the user's name for
it; it can stay a personal space). First run initializes everything into
one named Docker volume — data dirs, per-org databases, a self-signed TLS
keypair. Re-running is safe; initialization is idempotent.

**Path B — published image, cryptographically verified:**

```bash
# From the checkout: verify the immutable reference against the project key,
# then run exactly what was verified.
./deploy/verify-image.sh "$AUTONOMY_IMAGE"     # image@sha256:... only
AUTONOMY_IMAGE=<repo/image@sha256:...> docker compose pull
AUTONOMY_IMAGE=<repo/image@sha256:...> AUTONOMY_FIRST_ORG=myorg \
  docker compose up -d --no-build
```

`verify-image.sh` refuses mutable tags outright and verifies the cosign
signature against the project public key in the checkout — the registry
that hosts the image is a delivery channel, never an authority.

**Joining an existing organization instead** (the user has an invitation
code): don't found — join. The invitation rides install arguments, exactly
as DEPLOY.md documents:

```bash
docker run \
  -e AUTONOMY_INVITE="$INVITATION" \
  --mount source=autonomy-data,destination=/app/data \
  -p 8080:8080 autonomy-dashboard:local
```

`AUTONOMY_FIRST_ORG` and `AUTONOMY_INVITE` are mutually exclusive. A join
that needs approval parks safely and resumes on restart with the same
invitation — approvals are asynchronous, so expect to continue setup while
it's pending (§6a). There is no production mounted-personal-password variant.
With no one-time stdin password, a headless start may validate and stage the
invitation, but must not mint an identity or membership claim. The separately
guarded file input used by the multi-node test harness is test automation only
and is not an installation interface.

**What will NOT happen, on either path:** no account creation, no license
check, no telemetry, no CDN at runtime. The only external fetches are
anonymous build-time ones (base image, PyPI wheels, the tailwind binary),
each overridable to mirrors via `AUTONOMY_BASE_IMAGE` /
`AUTONOMY_TAILWIND_URL`. Verify all of this — §5.

## 5. Claims and receipts

Do not take this document's word for anything above. The claim table with
one runnable receipt per claim is at [/install/verify.md](/install/verify.md):
no-phone-home, no-CDN, signed image, one-volume sovereignty, offline
capability, no mandated registry. Run what's cheap now (the CSP check and
the CDN test take seconds), show the output, and offer the rest.

## 6. Identity — hand the human to the browser

Open `https://localhost:8080`. The **Get started** flow runs in the
browser: the user creates their personal identity (a key generated
client-side, protected by a passphrase they choose), enrolls a passkey,
and lands in their org. Your move is to *wait*. Do not ask for, accept,
or transcribe the passphrase or recovery material — if the user tries to
paste it to you, decline and say why. When they say they're in, continue.

### 6a. Invitation in hand? Join first.

The moment identity exists, submit the invitation claim — before any other
enrichment. The claim takes a v2 invitation CODE, not the URL: the literal
URL-to-code conversion is in [/install/paths/invite.md](/install/paths/invite.md). Joining an established org grants its entire knowledge graph
and its workspaces at once, which makes join-first the fastest route from
empty node to working estate. The claim may wait on a human approver on
the org side; that clock only runs after you submit, so submit early,
track "watch for admission," and overlap the rest of the setup.

## 7. Sessions — the handover point

The moment worth designing for: the user watching their own agent work in
a live session, in their browser, on their node. To get there: connect
their most active repository as a workspace (dashboard → Workspaces), and
provision session credentials — Autonomy sessions run Claude Code or Codex
inside containers, and the node needs a working credential for that. Use
the credential the user already has (you're running on it right now);
the dashboard's credential settings accept a setup token. Consume the
existing mechanism as-is; never invent a new secrets path.

Then launch a workspace session for the remaining INNER tasks and pass the
baton: write a short note into the new graph — what you observed, what the
user chose, to-do state — and tell the inner session to read it first.
Two agents sharing memory through the graph is the product's persistence
claim, demonstrated on day one. If session credentials can't be provisioned
yet, skip the relay: you complete the inner tasks from here, slower. The
relay is an optimization, never a dependency.

## 8. Garden paths — pick with what you know

Present a recommendation, not a menu-first. Full playbooks under
[/install/paths/](/install/paths/solo.md):

- **[Solo knowledge base](/install/paths/solo.md)** — personal org, graph
  seeded from their own work. Default for a solo developer.
- **[Team org](/install/paths/team.md)** — found the org, mint invitations
  for the teammates you saw in the commit history.
- **[Join by invitation](/install/paths/invite.md)** — they arrived
  invited; §6a already handled the claim; this is what's next.
- **[Agent workbench](/install/paths/workbench.md)** — sessions, beads,
  and dispatch running agents over their repositories.
- **[Publish & share](/install/paths/publish.md)** — mint share links;
  publish notes and artifacts to people who don't run Autonomy.

Seeding the graph is propose-not-act: survey their recent work, PROPOSE
the notes and structure you'd create, and create only what they approve.

## 9. Finish

From the inner session (or here): write a **"how I set this up"** note
into their graph — choices made, receipts collected, commands run. It's
the graph's first searchable content and teaches the loop by existing.
Then, with consent (it modifies your own harness config), install the
`autonomy` skill so you remain a competent Autonomy client after today:
fetch [/install/skill.md](/install/skill.md) and follow its placement
instructions. Then recede — the node and its dashboard carry on from here.

**Presentation, throughout:** show progress as a simple redrawn checklist
frame between steps — the format and tone guide is
[/install/present.md](/install/present.md). Decent progress indication is
the floor; charm is optional.

## 10. Troubleshooting and where things live

- All state lives in the `autonomy-data` volume (`/app/data` in the
  container). Back up the volume, you've backed up the deployment;
  `python3 -m tools.portability snapshot|restore` is the supported
  quiesced path, and restoring the volume elsewhere makes that machine
  the same identity.
- TLS is self-signed by default; `DASHBOARD_TLS=off` behind your own
  proxy. Port: `DASHBOARD_PORT` (default 8080).
- The optional beads issue-tracker backend is a separate compose profile
  (`docker compose --profile beads up -d`); without it the dashboard runs
  fine with empty beads surfaces.
- Deep reference: `DEPLOY.md` in the checkout — this document's facts are
  drift-tested against it, and where they could ever disagree, DEPLOY.md
  and the checkout win.
