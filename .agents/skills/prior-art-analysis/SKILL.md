---
name: prior-art-analysis
description: Find and evaluate existing internal and external solutions before designing or implementing non-trivial functionality. Use before proposing a new feature, service, schema, store, protocol, control plane, authentication or cryptographic mechanism, reusable UI primitive, workflow abstraction, agent skill, or bead specification, and whenever the requested capability may already exist under another name.
---

# Prior Art Analysis

Treat prior art as a design gate, not background reading. Do not introduce a
new primitive until the evidence shows why an existing one cannot be consumed
or extended.

## Run the gate

1. State the need without naming a preferred solution. Search for the problem,
   not only the proposed component name.
2. Search every relevant local surface:
   - source and tests with `rg` / `rg --files`;
   - graph notes, sessions, and beads with one high-signal term at a time;
   - open and closed work in `bd`;
   - product catalogs or registries when the task creates a named artifact.
3. Search synonyms, predecessor names, and adjacent domains. A mechanism may
   already exist for another product, as an internal helper, or only as a
   landed foundation beneath an aspirational design.
4. Read the strongest candidates completely. Include graph-note comments and
   the trailer. Verify current status in code, tests, commits, or the running
   system; a design note is not proof that an implementation exists.
5. Consult current external primary sources when the decision depends on a
   third-party API, standard, algorithm, provider, or legal/operational rule.
   External novelty does not excuse skipping internal reuse.
6. Compare candidates against the actual requirements. For each candidate,
   record:
   - evidence and current state (`live`, `landed`, `partial`, `design`, or
     `stale`);
   - the seam that can be reused;
   - what fits and what remains missing;
   - the decision: `consume`, `extend`, `replace`, or `reject`, with a reason.
7. Choose the narrowest delta. Prefer consuming a working primitive, then
   extending a shared primitive, then replacing one with a migration plan.
   Create a new mechanism only when the comparison demonstrates the gap.

## Required output

Before a design note, bead, or implementation that adds architecture, include
a short **Prior art** section containing:

- search scope and the useful terms;
- candidates and evidence references;
- the reusable seam selected;
- the precise missing delta;
- rejected candidates and the requirement each fails.

If no candidate survives, state what was searched. “Nothing exists” is not
supported by one query or by an empty result: graph visibility is org-scoped,
names drift, and peer notes may be unpublished.

## Proportionality

Keep the pass small for a local, well-bounded defect in a known function. Make
it deep for a new schema, identity or authorization rule, cryptography,
storage, synchronization, billing, control plane, or reusable abstraction.
This gate composes with every domain skill; another skill never replaces it.
