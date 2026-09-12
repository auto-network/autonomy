# Product workflow simulation — design of record

Operator requirements, session auto-0911-184859, 2026-09-12.
This supersedes the database-seeded harness and the manually orchestrated API scenario.

This simulation tests whether the product performs its own user workflows.
The harness may start isolated services, operate visible UI controls through
agent-browser, wait for events, and capture evidence. It must never perform,
replace, repair, or bypass a product transition. If the product does not perform
the transition, the test fails.

Before every change state: change, requirement served, and boundary (environment,
UI operation, or observation). A product fix is separate from a harness change.

- Real production dashboard, registry/relay, connector, ledger and vault code.
- Fresh independent data volumes and RAM-backed key caches for each machine.
- All product setup through UI, including profiles, image uploads and organizations.
- Browser JavaScript only locates visible controls, sets input values, dispatches
  normal input events, calls DOM click, waits for observable UI events, and reads UI.
- No injected HTTP/WebSocket calls, application imports, ceremony calls, application
  state writes, server function calls, CLI product operations, or database access.
- No mocks, response replacements, copied product state, sleeps, or polling loops.
- DOM controls must be unique, visible and enabled. Listeners precede actions.
  Timeouts bound failures only.
- Automatically save screenshots after observed UI transitions, and on failure.
  Exclude passwords, invitation fragments and private material from evidence.
- A failed or incomplete workflow never counts as a passed scenario. Keep timings
  and report the exact completed boundary.
- Organization scenario: independent people enroll; Alice creates the organization
  and publishes an invite; Bob receives the actual invitation and joins through
  the product; approval follows actual policy; data synchronizes; profiles and
  organization presentation render correctly. Subsequent requested coverage adds
  a third member, removes the second, and verifies ledger and vault generations.
- Personal fleet is a separate scenario for one person's machines, enrollment,
  note synchronization, key setup, and machine removal.
- No invented reboot/restore scenarios, negative tests, or self-audit subsystem.

First gates: real services healthy; Alice enrollment and initial session; profiles;
organization UI setup; invitation publication; Bob preview/admission; synchronization.
Successful UI screenshots alone do not prove later transport or synchronization.
