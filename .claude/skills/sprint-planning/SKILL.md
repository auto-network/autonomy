---
name: sprint-planning
description: Convert a product goal into a bounded sprint bet whose feature entries are engineering-ready for feature-planning.
user_invocable: true
---

# Sprint Planning

Sprint-planning is the level above feature-planning. It decides what valuable outcome the sprint buys, how much appetite it receives, what is explicitly excluded, and which feature entries must happen in what order. It does not copy the backlog and it does not prematurely choose libraries or implementation designs.

The quality failure this skill prevents is a direction-sketch: a codename, a desired component, and "add tests" with no reason, measurable requirements, evaluated uncertainty, or closing evidence.

## Output and handoff

Publish the canonical sprint plan as a graph note, not a repository file. The plan produces:

1. A sprint bet with north star, acceptance story, appetite, scope, sequence, gates, deferrals, and operator decisions.
2. An ordered feature list with readiness cards.
3. A lightweight outcome-to-gate trace, including material unknowns that feature-planning must resolve.

Each selected entry then goes through:

1. Feature-planning at graph://f2dff077-3c6: requirements and measurable non-functional requirements before solutions, resulting in a decision-note and concrete design.
2. Design Studio, when a user-visible surface is involved.
3. Bead polishing at graph://f6c6c43e-24a: the final work-item gate.
4. Implementation, testing, and cross-model attack.

Knowledge belongs in graph notes. Polished beads link the decision-note and design. Code is written later in git.

## Independent engineering basis

This skill uses the following authoritative references, checked independently:

- The official Scrum Guide defines Sprint Planning around why, what, and how; the Sprint Goal expresses why the sprint is valuable, and the Definition of Done provides the shared quality threshold: https://scrumguides.org/docs/scrumguide/v2020/2020-Scrum-Guide-US.pdf
- Shape Up's appetite and betting model treats appetite as a deliberate cycle-level constraint rather than an estimate of an unbounded backlog: https://basecamp.com/shapeup/1.1-chapter-02
- INCOSE's Guide to Writing Requirements requires measurable performance targets, explicit ranges, and explicit temporal dependencies; words such as "fast," "eventually," and "user-friendly" are not measurable requirements: https://www.incose.org/docs/default-source/working-groups/requirements-wg/guidetowritingrequirements/incose_rwg_gtwr_v4_summary_sheet.pdf
- NASA's Systems Engineering Handbook separates verification of requirement compliance from validation that the system solves the stakeholder need in its intended environment: https://www.nasa.gov/wp-content/uploads/2018/09/nasa_systems_engineering_handbook_0.pdf
- MADR requires context, decision drivers, considered options, an outcome, consequences, and confirmation; important design choices should preserve their rationale: https://adr.github.io/madr/decisions/adr-template.html

These sources support the process, not a claim that Scrum or Shape Up alone supplies our project-specific gates.

## Two appetites, not one

Set:

- Sprint appetite: the total budget for the end-to-end outcome. State a time/complexity boundary and the maximum acceptable number of major unknowns.
- Entry appetite: the bounded size of each feature-planning/implementation unit. A unit that crosses substrate, schema migration, new visual surface, or unrelated polish is split or explicitly justified.

Appetite is chosen before estimating implementation. If a feature cannot fit, reduce scope or defer it; do not silently increase the bet.

## Process

### 1. Establish the bet

Write the product vision, sprint north star, why now, sprint appetite, concrete acceptance story, and final gates. The acceptance story must describe a real user/operator lifecycle ending in observable evidence.

### 2. Survey and challenge

Run the `prior-art-analysis` skill at sprint breadth before choosing entries.
The survey must distinguish live/landed mechanisms from partial, design-only,
and stale records, and identify reusable seams rather than merely collecting
similarly named work.

Read current graph notes, source documents, code boundaries, landed commits, beads, dependency trees, runbooks, designs, and acceptance evidence. Survey by outcome, not prefix. For each candidate record:

- user/operator/system outcome and why omission matters;
- current readiness, provenance, owner, and environment;
- direct dependencies and downstream consumers;
- functional behavior and failure behavior;
- likely scale, latency, concurrency, reliability, security, portability, cost, or operability constraints;
- existing architecture it must consume or modify;
- unknowns that require a research spike;
- whether another candidate duplicates it.

Reject stale summaries when current code, data, or dependency trees disagree.

### 3. Identify decision-driving requirements

At sprint level, do not write the full feature specification. Identify only the functional or non-functional drivers that could change scope, sequencing, architecture, or acceptance. Record:

- the behavior or quality at risk;
- why it matters to the sprint outcome;
- the decision or gate it affects;
- whether the number is known, unknown, or an explicit operator decision.

Do not pretend that "fast," "scalable," or "secure" is complete. If a missing number blocks a choice, add a small research entry or send it to feature-planning as a named unresolved requirement. Do not choose a library here. Feature-planning converts the drivers into measurable requirements, evaluates options, and records the decision.

The sprint plan should be brief enough to read in one sitting. Completeness means every important question has an owner and a next stage, not that every detail is duplicated here.

### 4. Choose scope

Create explicit lists:

- In scope: necessary for the north star or a gate.
- Deferred: valuable but not required, with rationale.
- Operator decisions: choices that alter scope, architecture, commitment, or acceptance.
- Research/spikes: bounded work required to remove a decision-blocking unknown.

A feature is not in scope merely because it is interesting or already has a bead.

### 5. Sequence the work

Build a dependency graph from current evidence. Distinguish:

- can begin versus blocks a gate;
- implementation dependencies versus evidence dependencies;
- serial work versus parallel design/research tracks;
- a feature versus its final acceptance evidence.

Mark the critical path. Assign one owner and one file/ownership boundary only when implementation begins. Never assign overlapping work to eager sessions.

### 6. Define gates and evidence

For each gate specify outcome, topology/environment, prerequisites, evidence shape, failure behavior, and what constitutes a pass. Tests support a gate; they do not replace end-to-end validation. Maintain this lightweight trace:

goal -> feature entry -> gate -> evidence

Every material unknown must point to the feature-planning or research step that resolves it. Every gate must trace back to the acceptance story.

### 7. Prepare feature entries

Each entry receives a readiness card:

- name and outcome;
- valuable to;
- entry appetite;
- decision-driving requirements and known unknowns;
- consumes, produces, and modifies;
- dependencies and downstream consumers;
- constraints and assumptions;
- success evidence at sprint level;
- likely design/architecture decisions for feature-planning;
- owner boundary to decide later;
- open decision;
- readiness: candidate or ready-for-feature-planning.

An entry is ready for feature-planning only when another engineer can begin the decision work without rediscovering its purpose, constraints, appetite, measurable success, dependencies, or unknowns. A codename plus a direction is not ready.

Do not label entries ready-for-implementation. That requires the feature-planning decision-note, approved design where applicable, and polished bead.

### 8. Handoff chain

For every ordered entry:

1. Run feature-planning. Its decision-note must include context, requirements and numbers, constraints, options, trade-offs, decision, concrete interfaces/data flow, edge cases, security/failure behavior, scope, and test plan.
2. For UI, obtain an approved Design Studio design and rewrite the bead body to make the design authoritative.
3. Polish the bead using graph://f6c6c43e-24a, including structural boundaries, state matrix, L2.B acceptance where applicable, dependencies, and one owner.
4. Implement only after the bead is accepted.
5. Require a different model to attack the result. Self-review is not Definition of Done.

## Plan shape

Use this order:

1. Status and scope
2. North star and acceptance story
3. Sprint appetite and why now
4. Decision-driving requirements and outcome-to-gate trace
5. In scope, deferred, research, operator decisions
6. Dependency sequence and parallel tracks
7. Acceptance gates
8. Ordered feature-entry readiness cards
9. Risks, seams, assumptions, and unresolved unknowns
10. Handoff checklist

Use plain prose and expand codenames on first use. The legibility test is whether an engineer who knows the product but not this sprint can explain the outcome, budget, exclusions, dependencies, unresolved decisions, and closing evidence after one read.

## Definitions

### Sprint Definition of Ready

The plan has a concrete north star and acceptance story; an explicit appetite; rationalized scope and deferrals; decision-driving requirements and named unknowns; visible critical path and parallel work; gates with evidence; isolated operator decisions; and every selected entry marked ready-for-feature-planning. The plan is published as a graph note and cross-reviewed by a different model.

### Sprint Definition of Done

Operator decisions are resolved or accepted as assumptions; each selected entry has a feature-planning decision-note; each UI entry has the required approved design; each resulting bead passes bead polishing; owners and sequence are recorded; and the sprint closes against its gates, requirements, and evidence, not its bead count.

## Self-check

Before publishing, ask:

- Did we choose a bet rather than copy the backlog?
- Is appetite a real constraint?
- Does every material quality claim have a number, a named feature-planning requirement, or a bounded research entry?
- Did we avoid selecting tools before requirements and options?
- Are dependencies grounded in current evidence?
- Does every requirement map to verification evidence?
- Did we separate implementation from acceptance evidence?
- Did we name deliberate exclusions and unresolved operator calls?
- Could another engineer start feature-planning without clarification?
- Has a different model attacked the plan?

If any answer is no, keep the plan in draft and name the gap.
