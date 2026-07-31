---
name: feature-planning
description: Use BEFORE designing or beading any non-trivial feature. Turns "we should build X" into a decided, testable design a different engineer can implement without re-deciding anything. The rule is requirements-as-testable-scenarios before solutions — no library, tool, or design is named until the quality-attribute scenarios (with numbers) that decide it are written. Output is a decision-note in the graph plus a polished bead.
user_invocable: true
---

# Feature planning

You are at the front of the engineering funnel: turning a vague intent into a decided design. The failure this prevents is the **direction-sketch** — a bead that names a component ("a local VM backend"), adds "with tests", and calls it a spec, having chosen a solution before the requirements that decide it exist. The cure is one rule with one technique behind it.

## The one rule: testable requirements before solutions

No library, framework, tool, or design is named until (a) the use cases and (b) the **quality-attribute scenarios that decide the choice** are written down. Every option is then scored against those scenarios, and the decision cites the specific driver each rejected option fails.

Shape Up states the discipline precisely: *"Estimates start with a design and end with a number. Appetites start with a number and end with a design."* (Basecamp, *Shape Up*, ch. 3.) You set the number — the appetite and the response measures — first; the design falls out. If you catch yourself writing "we'll use multipass / Postgres / a queue" before the scenarios exist, stop: you are estimating a design instead of shaping from requirements.

Real design is not actually this linear — you will loop. Parnas & Clements ("A Rational Design Process: How and Why to Fake It", IEEE TSE 1986) is the honest account: you cannot design rationally, but you *document it as if you had*, because the rational write-up is what the next engineer needs. So: **loop while you work; the decision-note reads as if you didn't.**

## The technique: a quality-attribute scenario turns "fast" into a test

The reason "make it fast / secure / reliable" never decides anything is that it isn't measurable. The SEI's **quality-attribute scenario** (Bass, Clements & Kazman, *Software Architecture in Practice*) fixes that with six parts — write them and you have a requirement a test can fail on:

1. **Source** — who/what generates the trigger.
2. **Stimulus** — the condition that demands a response.
3. **Environment** — the conditions it holds under (normal, peak, startup, degraded — this is where the load and the platform constraints live).
4. **Artifact** — the part of the system stimulated.
5. **Response** — what must happen.
6. **Response measure** — the number that makes it testable.

Weak: "provisioning a test VM should be fast." Scenario: *"When the CI job requests a clean machine (stimulus) from the loop (source) on a stock GitHub-hosted runner with no nested virtualization (environment), the machine (artifact) is exec-reachable (response) within 90 s at p95 over 20 consecutive runs (response measure)."* The second one decides the library; the first can't. **If you cannot write the response measure, you cannot choose the tool.**

Not every requirement needs this. Write full scenarios for the **architecturally significant** ones — the requirements whose cost of change is high (Chen, Ali Babar & Nuseibeh, "Characterizing Architecturally Significant Requirements", IEEE Software 2013). The heuristic: it has wide impact, forces a tradeoff, is strict/non-negotiable, breaks an assumption, or is technically hard. A quality attribute at load is almost always significant; "button is teal" is not.

## The process (a loop, documented as an order)

1. **Problem.** What problem, for whom, why now, cost of not doing it. One line a user would care about (Amazon's working-backwards test). Interrogate it; the thin bead is often an *unresolved* problem, not a lazy author.
2. **Prior art & reuse.** Search existing decision-notes and the codebase first. Don't re-decide or rebuild what exists; name what you'll consume.
3. **Use cases.** Concrete scenarios: who, for what, when, where. "CI provisions one clean machine per PR with no cloud credentials" is a use case; "we might need VMs" is not.
4. **Appetite.** The budget you *choose* — time/effort/complexity this is worth — set before you explore options, so it bounds them.
5. **Requirements.** Functional (what it must do) and **quality requirements** as scenarios. Walk the ISO/IEC 25010 quality characteristics as a checklist so you skip none that matter: performance efficiency, reliability, security, compatibility, interaction capability, maintainability, flexibility/portability, safety. Write a full six-part scenario for each architecturally significant one.
6. **Constraints.** What's fixed — platform, dependencies, sovereignty, the existing architecture it must fit.
7. **Stakeholder decisions.** List the choices you cannot make alone — scope, appetite confirmation, a semi-permanent or costly commitment, an accepted risk. Get them **before** the design hardens, not after. Designing past an unresolved stakeholder decision is how you build the wrong thing confidently.
8. **Options, scored against the drivers.** Enumerate the real candidates. Score each against the scenario response measures and the constraints — not generic pros/cons. Name the **tradeoff points** (a property that helps one quality attribute and hurts another — e.g. container vs VM trades CI-portability against fidelity; that's the decision, so make it explicit). Every rejected option names the driver it fails.
9. **Decision.** The choice, with rationale traceable to a specific driver.
10. **Concrete design.** Data model; interfaces (endpoints, request/response shapes); the flow as a sequence; edge cases; failure modes; the security model — enough that a different engineer implements without re-deciding.
11. **Scope & test plan.** In/out. How each requirement is checked — and distinguish **verification** (the response measure is met) from **validation** (the real need is solved in the real environment); the acceptance gates are validations (NASA SE Handbook's distinction). Tie tests to the scenarios.
12. **Write it up** — the decision-note (graph) + the bead.

## The artifact: a decision-note in the graph (MADR)

Knowledge goes in the graph, not a repo file — git is for code. Use the MADR 4 shape (adr.github.io/madr; origin: Nygard, "Documenting Architecture Decisions", 2011): **Context & problem statement · Decision drivers** (the scenarios, with numbers) **· Considered options · Decision outcome** (+ consequences + confirmation) **· Pros and cons of the options · More information.** Graph versioning gives you the ADR "supersede, don't edit" property for free; provenance links note → bead → session → commit. The bead links the note; [bead polishing](graph://f6c6c43e-24a) is the final gate on the work item.

## Cutting the epic and its beads (exact commands)

A non-trivial feature is an epic with child beads, not one bead. Create them and wire the graph so the build order is machine-checkable; do not leave dependencies implied in prose.

```
# Epic and children — --source links each to the decision note (provenance).
graph bead "<epic title>"  -t epic -p 1 --source <decision-note-id> -d - < epic.md
graph bead "<child title>" -t task -p 1 --source <decision-note-id> -d - < child.md   # once per child
# Hierarchy: attach each child to the epic.
bd update <child-id> --parent <epic-id> --add-label readiness:specified,<area>
# Dependency edges: the BLOCKER comes first. Read it as "<blocker> blocks <blocked>",
# i.e. <blocked> cannot start until <blocker> is done.
bd dep <blocker-id> --blocks <blocked-id>
# Verify before you stop: the capstone shows every prerequisite, and the foundations are ready.
bd dep tree <capstone-child-id>   # must render the full prerequisite chain, capstone [BLOCKED]
bd ready -n 0 | grep <epic-id>    # the no-dependency foundation beads must appear [open], unblocked
```

Rules that make the wiring correct: every child names its blockers with `bd dep` (an edge, never a sentence); the foundation beads (no blockers) are the entry points and must show ready; the capstone (end-to-end validation) depends transitively on all the rest; and `bd dep <blocker> --blocks <blocked>` puts the blocker first — reversing it inverts the build order silently.

## Enforcement — so the rule isn't a slogan

- **Structural:** the note's drivers physically precede its options. A reviewer rejects any option-vs-option comparison that doesn't reference a written driver.
- **Truth to the data model:** the design reflects only what the system actually does or can produce — verified against the real code, never a fabricated state or an untrue claim.
- **Cross-model attack (the teeth):** a *different* model runs this checklist, then tries to break it — (1) every considered option scored against a written scenario; (2) every significant requirement has a response measure, not an adjective; (3) the fabricated-state check ran against real code; (4) the stakeholder decisions are resolved, not assumed; then (5) find the missing requirement / unhandled edge / wrong tradeoff. Reading it is not reviewing it.

## The self-test before you stop

Hand it to a different engineer who knows the product but not this job. Could they build it without one clarifying question or one re-made decision? If not, the thin step is usually a missing response measure (5), an option never scored (8), or an unresolved stakeholder decision (7). Iterate until the answer is yes — that bar is the definition of done for this skill.

## Grounding (verified primary sources)

- Quality-attribute scenario (6 parts) — Bass, Clements & Kazman, *Software Architecture in Practice*, SEI Series; part names confirmed in CMU/SEI-2003-TR-016 (QAW).
- Quality-requirements drive architecture; are under-captured — Clements & Bass, CMU/SEI-2010-TN-018.
- Architecturally significant requirements — Chen, Ali Babar & Nuseibeh, IEEE Software 30(2), 2013.
- Quality taxonomy checklist — ISO/IEC 25010:2023 (product quality model).
- Well-formed requirement (verifiable, singular, unambiguous, feasible) — ISO/IEC/IEEE 29148:2018.
- Appetite / "estimates vs appetites" / fixed-time-variable-scope — Shape Up (Basecamp), ch. 3 & 8.
- Decision records — Nygard 2011; MADR 4 (adr.github.io/madr). Faking-it rationale — Parnas & Clements, IEEE TSE 1986.
- Verification vs validation — NASA Systems Engineering Handbook.