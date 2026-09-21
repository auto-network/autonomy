# Product workflow simulation

The acceptance test for this product is a browser driving the real user
interface against real services. Two scenarios:

```bash
node deploy/harness/workflow.mjs       # organization: invite, join, approve, sync
node deploy/harness/fleet-workflow.mjs # personal fleet: enroll a second machine
```

Each run starts an isolated registry and relay plus one dashboard per person,
builds the node image from the current worktree including uncommitted edits,
provisions trusted local HTTPS, and drives every product transition through
visible controls with `agent-browser`. Evidence lands in the output directory
you pass: numbered screenshots at each observed transition, both dashboards'
fleet and connector logs, and a `result.json` carrying every step with its
duration.

## The rule

`WORKFLOW-REQUIREMENTS.md` in this directory is the design of record, written
by the operator on 2026-09-12. The sentence that governs everything else:

> This simulation tests whether the product performs its own user workflows.
> The harness may start isolated services, operate visible UI controls through
> agent-browser, wait for events, and capture evidence. It must never perform,
> replace, repair, or bypass a product transition. If the product does not
> perform the transition, the test fails.

Read it before changing anything here. A harness change and a product fix are
separate changes, and the boundary between them is the point of the document.

## What each scenario proves

**Organization.** Two people enroll independently with their own identities and
profiles. Alice creates an organization and publishes an invitation. Bob
receives the real invitation and joins through the product; admission follows
the organization's actual approval policy. Alice's charter edit and her profile
photo then appear on Bob's dashboard by synchronization, and both people lock
and sign on again.

**Personal fleet.** Alice creates a note, publishes a fleet invitation, and
authorizes a second machine through Central. The second machine completes its
first unlock and synchronizes, and the note's content matches exactly. The note
is then published as a link, read through the relay by a third browser, and
read again after the publishing dashboard is stopped, which is what proves the
second machine serves it.

## Options

Ports are chosen automatically and can be pinned with `SIM_RELAY_PORT`,
`SIM_ALICE_PORT` and `SIM_BOB_PORT`. `SIM_ISOLATE_DASHBOARDS=1` puts the two
dashboards on separate networks so they can reach each other only through the
relay. Pass an output directory as the first argument.

Requirements: a Linux Docker host with the Compose plugin, Node 22,
`agent-browser`, `tailwindcss`, `rsync`, `openssl`, and `certutil` from
`libnss3-tools` so the browser trusts the generated certificate.

## What used to live here

A fixture-seeded multi-node ladder, a paced demo driver built on it, and a
production TURN acceptance harness were removed on 2026-09-20. All three
established their state by writing directly into the stores instead of using
the product, which is the practice the document above exists to forbid, and all
three had been failing silently for weeks because nothing ran them. The
coverage worth keeping is elsewhere: volume snapshot and restore in
`tools/tests/test_portability.py`, and serving from a second machine after the
publisher stops in the fleet scenario above.
