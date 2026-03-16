# Focus: Agent Orchestration Requirements

## What Focus Is

The abstract job tracking, queuing, prioritization, allocation, and agent dispatch command center. Focus orchestrates *all* autonomous work across the Autonomy Network.

Focus operates at a high abstraction level — it is NOT designed for specific job types. Domain-specific logic lives in the agents/librarians that Focus dispatches, not in Focus itself.

## What Focus Must Do

### Core Responsibilities

1. **Work Item Management** — Create, track, prioritize, and dependency-link work items (beads/tasks)
2. **Environment Management** — Know what run environments exist, their capabilities, resource limits, and current load
3. **Dispatch** — Match ready work items to available environments and agents, launch execution
4. **Monitoring** — Track active agent sessions: progress, health, resource consumption, elapsed time
5. **Tracing** — Full audit trail from work item creation → dispatch → execution → result
6. **Result Collection** — Gather outputs from completed agent runs, validate, route back into the knowledge graph
7. **Prioritization** — Dynamic priority based on urgency, dependencies, resource availability, and user directives

### What Focus Does NOT Do

- Hold the full vision (that's the knowledge graph)
- Implement domain-specific logic (that's agent prompts)
- Manage authentication/networking (that's the mesh layer, established before agents boot)
- Generate playbooks, validate specs, write code (those are job types Focus dispatches)

## Requirements by Area

### R1: Work Items (Beads)

- R1.1: Unique, content-addressable IDs (hash-based like Beads, or UUID)
- R1.2: Parent/child relationships (decomposition: goal → subtasks)
- R1.3: Dependency graph (A blocks B; B is ready when A completes)
- R1.4: Status lifecycle: `draft → ready → dispatched → running → completed | failed | blocked`
- R1.5: Priority levels with dynamic reprioritization
- R1.6: Tags/labels for filtering (project, audience, job-type)
- R1.7: Metadata: estimated cost, actual cost, token usage, wall time
- R1.8: Immutable history (every state change logged with timestamp)

### R2: Environments

- R2.1: Registry of available execution environments (local, container, remote)
- R2.2: Capability declarations per environment (what tools are available, what access)
- R2.3: Resource limits (max concurrent agents, memory, token budget)
- R2.4: Scoped access control — each environment has a `GRAPH_SCOPE` and tool set
- R2.5: Health checks — is the environment up, responsive, within resource limits?
- R2.6: Environment templates — spin up new environments from templates (docker-compose, etc.)

### R3: Dispatch

- R3.1: Match work items to environments based on requirements and capabilities
- R3.2: Configurable dispatch strategies (FIFO, priority, round-robin, affinity)
- R3.3: Concurrency control — max agents per environment, max total agents
- R3.4: Pre-flight checks before dispatch (environment healthy, resources available, dependencies met)
- R3.5: Dispatch should provide the agent with: scoped graph access, task description, output location
- R3.6: Retry policy for transient failures (API timeouts, container startup failures)

### R4: Monitoring

- R4.1: Real-time status of all dispatched work items
- R4.2: Agent heartbeat / progress indicators
- R4.3: Resource consumption tracking (tokens, wall time, API calls)
- R4.4: Alerting on: stuck agents, runaway token consumption, exceeded time limits
- R4.5: Kill switch — ability to terminate any running agent
- R4.6: Dashboard / API for operational visibility

### R5: Tracing

- R5.1: Every state transition recorded with timestamp, actor, and reason
- R5.2: Link from work item → dispatched agent session → session JSONL in graph
- R5.3: Parent/child trace propagation (if Focus dispatches an agent that spawns subagents)
- R5.4: Cost attribution — total tokens/dollars per work item, per project, per time period
- R5.5: Exportable trace format (OpenTelemetry-compatible?)

### R6: Result Collection

- R6.1: Structured output format from agents (not just free text)
- R6.2: Validation of outputs before marking work item complete
- R6.3: Automatic ingestion of agent outputs into the knowledge graph
- R6.4: Diff/patch extraction for code-producing agents
- R6.5: Decision capture — every agent produces a machine-readable decision (CONTINUE, DONE, BLOCKED, FAILED)

### R7: Prioritization

- R7.1: Static priority assignment (P0-P3)
- R7.2: Dynamic priority adjustment based on: age, dependency chains, user directives
- R7.3: Starvation prevention — low-priority items eventually get scheduled
- R7.4: User override — manual priority bump/suppress
- R7.5: Budget-aware — don't dispatch $50 research tasks when daily budget is $10

## Constraints

### From Existing Architecture

- Must work with SQLite (not a heavy database dependency)
- Must respect sovereignty line — Focus runs locally, user-controlled
- Must integrate with existing `graph` CLI (scoped access, playbooks, sessions)
- Must support the three-layer sandwich: Agent Layer → Tool Layer → Mesh Layer
- Agents get pre-authenticated environments; Focus handles setup before agent boot

### From ~/jira/ Lessons

- Container isolation is mandatory for concurrent agent execution
- Side effects (git push, JIRA post) must be separated from agent analysis
- Agents WILL ignore stop instructions — structural constraints > behavioral instructions
- Transient API failures are normal; retry without abandoning accumulated work
- Decision files are the clean interface between agents and orchestrator
- The orchestrator validates outputs, not just file existence
- Bake workspaces into images; never download at runtime

### Non-Goals (for now)

- Multi-node distributed execution (single machine first)
- Real-time agent-to-agent messaging during execution (post-completion coordination first)
- GUI (CLI/API first, web dashboard later)
- Billing/metering (track costs but don't enforce billing)

## Prior Art to Evaluate

| System | Relevance | Notes |
|--------|-----------|-------|
| Beads / GasTown (Yegge) | High | Distributed graph issue tracker designed for AI agents. SQLite+JSONL. |
| ~/jira/ orchestration | High | Our own battle-tested patterns. Too purpose-built but proven. |
| CrewAI | Medium | Multi-agent orchestration, role-based. |
| AutoGen (Microsoft) | Medium | Multi-agent conversation framework. |
| LangGraph | Medium | Stateful agent workflows with cycles. |
| Temporal | High | Production workflow orchestration. Durable execution. |
| Prefect | Medium | Python-native workflow orchestration. |
| Modal | Medium | Serverless compute, sandboxed execution. |
| E2B | Medium | Sandboxed code execution for AI. |
| Julep | Low-Med | Agent execution platform. |

## Open Questions

1. **Build vs adopt vs compose?** — Is there a system we can adopt wholesale, or do we compose Focus from multiple components?
2. **Beads as the work item format?** — Beads is designed exactly for this. Do we adopt it directly?
3. **Temporal for durable execution?** — Temporal solves dispatch, retry, monitoring, tracing. Heavy dependency though.
4. **How does Focus relate to the knowledge graph?** — Is Focus a consumer of the graph, or does it live inside it?
5. **What's the minimal useful Focus?** — What's the smallest thing we can build that starts delivering value?

---

*This document is a living requirements spec. It will be updated as market research and prototyping reveal what's viable.*

*See also: [Playbook catalog](../tools/graph/playbooks.py) for the first primitive Focus-like work manifest.*
