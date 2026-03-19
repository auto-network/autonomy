# Librarian Agents — Design Document

## Overview

Librarians are agents that maintain knowledge quality through classification, linking, and lifecycle management. They don't write code — they read, tag, link, and organize. They are the custodians of the knowledge graph, the bead backlog, and the operational feedback loop.

## Vision

The local PoC proves the concept: can librarians improve knowledge quality and reduce human curation burden without going off the rails? The measurable outcomes:
- Do tagged pitfalls surface more relevantly than FTS matching?
- Do experience reports get processed into actionable beads without human intervention?
- Do beads move from idea to specified with correct file paths and dependencies?
- Does the taxonomy stay coherent or drift into garbage?

At scale, the graph becomes distributed across hundreds of users, each contributing to shared institutional knowledge. The taxonomy evolves from one project's codebase structure into a domain ontology. Librarians process contributions from all users, and feedback aggregates across all projects — a pitfall marked useful by 50 projects is validated knowledge. The local PoC architecture should not prevent this evolution, but the implementation focuses on proving value for a single user first.

## Why Librarians Exist

The Autonomy platform generates knowledge artifacts at high velocity: beads are created by agents and humans, notes are dropped during conversations, experience reports are written after every dispatch, sessions accumulate in the graph. Without curation, this becomes a junk drawer — duplicates pile up, pitfalls go untagged, beads sit at `readiness:idea` forever, experience reports are written and never read.

Today the human is the librarian. This doesn't scale. At 10k dispatches per day, each producing an experience report, discovered beads, and session logs, no human can review everything. The knowledge degrades — primers surface irrelevant pitfalls via loose FTS matching, beads lack file paths and acceptance criteria, duplicate work gets dispatched.

Librarians close the feedback loop. They process raw output into curated knowledge that makes the next dispatch better.

## Librarian Types

Each librarian type is a registered role with:
- A **name** and **description**
- A **static prompt** (role definition, tools, expected output format)
- A **dynamic primer builder** (generates task-specific context from the job payload)
- A **trigger** (which event enqueues the job — the event type determines the librarian, there's no routing decision)
- An optional **schedule** (for periodic jobs like taxonomy refresh)
- A **default model** (Opus by default, overridable to Sonnet/Haiku for cost testing)
- A **max_concurrent** limit (singleton for taxonomy, parallel for note processing)

### Experience Report Reviewer

**Trigger:** Dispatcher enqueues a job after collecting results from a completed dispatch. The dispatcher knows the output dir, bead ID, and report path — it passes these in the job payload.

**Input:** Experience report, decision.json, session JSONL, bead description

**What it does:**
- Reads the experience report for tooling bugs, pitfalls, and tool feedback
- Extracts pitfalls → creates graph notes (untagged — the note processor will tag them)
- Extracts tooling bugs → creates beads at readiness:idea
- Extracts discovered work not captured in decision.json → creates beads
- Reads pitfall feedback from decision.json (pitfalls_reviewed/pitfalls_useful/pitfalls_irrelevant fields)
- Detects recurring patterns across multiple reports → escalates priority

**Output:** New notes, new beads, pitfall feedback scores

### Taxonomy Maintainer

**Trigger:** Timer in the dispatcher loop checks if it's been 24+ hours since the last run and enqueues a job. Also triggered on-demand if the note processor encounters an unknown tag.

**Input:** Current taxonomy, repo file tree, bead epic structure, existing tags in use

**What it does:**
- Scans bead epics and their children for subsystem names
- Scans file paths mentioned in bead descriptions
- Scans the actual directory/file structure of the repo
- Scans existing tags in use across notes and beads
- Adds new tags when new subsystems emerge
- Retires tags when code is deleted or refactored
- Validates parent-child tag hierarchy matches the codebase structure
- Runs incrementally — diffs against previous state rather than full rescan

**Output:** Updated taxonomy in graph.db tags table

**Concurrency:** Singleton — only one instance runs at a time.

### Note Processor

**Trigger:** `graph note` enqueues a job after creating a note, passing the source_id in the payload.

**Input:** Raw note text, current taxonomy, existing notes library

**What it does:**
- Reads the raw note text and classifies it (pitfall, architecture decision, implementation note, etc.)
- Picks appropriate tags from the taxonomy vocabulary — does not invent tags
- Searches for duplicate or overlapping existing notes
- If duplicate: recommends merging or links to the existing note
- If unique: adds structured tags from the taxonomy
- Cleans up unclear text — adds specific file paths, reproduction steps, context
- Notes without taxonomy tags are invisible to bead primers, so this step is what makes notes discoverable

**Output:** Tagged and classified note in the graph

**Concurrency:** Multiple instances can run in parallel — each processes a different note.

### Bead Readiness Librarian

**Trigger:** `graph bead` or `bd create` enqueues a job after creating a bead at readiness:idea, passing the bead_id.

**Input:** Raw bead, current taxonomy, parent epics, sibling beads, graph context

**What it does:**
- Reads the bead title and description
- Searches for duplicates via bd find-duplicates and graph search
- If duplicate: links to existing bead, flags for human decision
- If unique: assigns to the correct parent epic based on taxonomy tags
- Wires dependencies by finding related beads in the same subsystem
- Fleshes out description: adds file paths from the taxonomy's file_patterns, adds acceptance criteria, adds platform context
- Validates against the parent epic vision — does this bead belong here?
- Promotes to readiness:draft or readiness:specified

**Output:** Enriched bead ready for human review

## Taxonomy

The taxonomy is the controlled vocabulary that connects pitfalls to beads. Both beads and notes share tags from this vocabulary. Tags are the join key — not free-text search.

### Schema (graph.db)

```sql
CREATE TABLE tags (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    parent TEXT REFERENCES tags(name),
    file_patterns TEXT,           -- JSON array of globs
    created_at DATETIME DEFAULT (datetime('now')),
    updated_at DATETIME DEFAULT (datetime('now')),
    status TEXT DEFAULT 'active'  -- 'active' or 'retired'
);

CREATE TABLE source_tags (
    source_id TEXT REFERENCES sources(id),
    tag TEXT REFERENCES tags(name),
    PRIMARY KEY (source_id, tag)
);
```

Bead tags are stored in the existing Dolt labels system — labels like `tag:sse`, `tag:dispatcher` — so they flow through the existing bd infrastructure without a new table.

### CLI

The taxonomy is a first-class data structure in the graph. Full CRUD via the `graph` command:

```
graph tags                              — list all active tags as tree
graph tags --flat                       — list without hierarchy
graph tags --json                       — machine-readable
graph tag create "sse" \
  --desc "Server-Sent Events" \
  --parent dashboard \
  --files "tools/dashboard/event_bus.py,tools/dashboard/server.py"
graph tag show "sse"                    — details + notes/beads using this tag
graph tag update "sse" --add-files "tools/dashboard/static/js/events.js"
graph tag retire "tailwind-cdn"         — mark inactive
graph tag search "event"               — find tags by name/description
```

### How Tags Flow

1. Agent creates a note via `graph note "..." --tags pitfall,sse`
2. If `sse` is in the taxonomy, `source_tags` entry is created
3. If `sse` is not in the taxonomy, the note is still created but a warning is printed and a job is enqueued for the note processor
4. Bead primer builds: bead has labels `tag:sse,tag:dashboard` → primer queries `source_tags` for notes with matching tags → relevant pitfalls surface
5. Taxonomy maintainer periodically validates that tags match the codebase

### Primer Retrieval (replacing FTS)

Current (broken — loose OR matching on title words):
```python
fts_q = _sanitize_fts_query(" ".join(title_words[:5]), or_mode=True)
```

Future (tag intersection):
```python
bead_tags = [l.replace("tag:", "") for l in bead["labels"] if l.startswith("tag:")]
pitfalls = db.conn.execute("""
    SELECT DISTINCT s.id, t.content, s.metadata
    FROM sources s
    JOIN source_tags st ON st.source_id = s.id
    JOIN thoughts t ON t.source_id = s.id
    WHERE st.tag IN ({})
    AND s.metadata LIKE '%pitfall%'
    ORDER BY s.created_at DESC LIMIT 5
""".format(",".join("?" * len(bead_tags))), bead_tags)
```

### Staging

Notes created without taxonomy tags are invisible to bead primers because primers retrieve by tag intersection. No staging table or reviewed flag needed — the absence of tags is the gate. The note processor adds tags, which is what makes notes discoverable.

## Job Queue

Events produce jobs. The dispatcher consumes them. Each event type determines exactly which librarian processes it — there is no routing decision.

### Schema (dispatch.db)

```sql
CREATE TABLE librarian_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,        -- 'review_report', 'process_note', 'refresh_taxonomy', 'readiness_triage'
    payload TEXT,                  -- JSON: context for this specific job
    status TEXT DEFAULT 'pending', -- pending, running, done, failed
    priority INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    librarian_type TEXT,          -- references the librarian type registry
    session_id TEXT,              -- links to the graph session for this run
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3
);
```

### Producers

Each producer knows exactly what job type to create:

```python
# Dispatcher, after collecting results:
enqueue("review_report", payload={
    "bead_id": agent.bead_id,
    "report_path": f"{agent.output_dir}/experience_report.md",
    "decision_path": f"{agent.output_dir}/decision.json",
    "run_id": run_id,
})

# graph note, after creating a note:
enqueue("process_note", payload={"source_id": new_note_id})

# graph bead / bd create, after creating a bead:
enqueue("readiness_triage", payload={"bead_id": new_bead_id})
```

### Scheduling

The dispatcher loop checks timers each cycle:
```python
last_run = get_last_completed("refresh_taxonomy")
if not last_run or (now - last_run) > timedelta(hours=24):
    enqueue("refresh_taxonomy")
```

### Failure Handling

- Failed jobs increment `attempts` counter and return to `pending`
- Jobs exceeding `max_attempts` move to `failed` status
- Failed jobs don't block other jobs of the same type
- The dashboard job queue view shows failed jobs for human review

## Agent Lifecycle

Librarians run as Claude sessions in containers with restricted permissions.

### Runtime

The PoC uses containerized Claude for all librarians — the Max subscription makes it incrementally free. Each librarian type specifies a default model (Opus) which can be overridden to Sonnet or Haiku for cost testing. The runtime is pluggable for the future (Codex, direct API calls, different LLMs) but the PoC only needs Claude containers.

### Permissions
- Repo: read-only mount
- Beads (bd): read-write (create beads, update status, set labels)
- Graph: read-write (create notes, add tags, link sources)
- No worktree, no git branch, no merge

### Dispatch Flow
1. Dispatcher checks the job queue each cycle alongside checking for approved beads
2. Picks up a pending job, looks up the librarian type in the registry
3. Builds the prompt: static role prompt (from `agents/librarians/{type}/prompt.md`) + dynamic task primer (from `agents/librarians/{type}/primer.py` using the job payload)
4. Launches container with librarian permissions
5. Polls for completion (same mechanism as implementation agents)
6. Collects results — no merge step, no worktree cleanup
7. Updates job status in the queue
8. Ingests session into graph

### Code Structure

```
agents/
  dispatcher/
    __init__.py
    main.py              — CLI entry point, main loop
    cycle.py             — bead dispatch cycle
    db.py                — dispatch_runs SQLite operations
    recovery.py          — recover_running_agents, reconcile_state
    jobs/
      queue.py           — job queue schema, enqueue, dequeue
      scheduler.py       — scheduled job definitions, timer checks
      runner.py          — librarian job lifecycle (launch, collect)
  librarians/
    registry.py          — type definitions, model defaults, concurrency limits
    experience_reviewer/
      prompt.md          — static role prompt
      primer.py          — builds task context from payload
    note_processor/
      prompt.md
      primer.py
    taxonomy_maintainer/
      prompt.md
      primer.py
    readiness_pipeline/
      prompt.md
      primer.py
```

### Adding a New Librarian Type
1. Create a directory under `agents/librarians/`
2. Write `prompt.md` — the static role definition
3. Write `primer.py` — a function that takes a job payload and returns primer text
4. Register in `registry.py` with name, trigger type, schedule, model, max_concurrent
5. Add the enqueue call to the appropriate producer (dispatcher, graph CLI, etc.)

## Dashboard Integration

### Librarian Types Page
- List of all registered librarian types
- For each: name, description, trigger type, schedule, default model, last run time, success rate
- Click into a type → see its static prompt, recent sessions, job history

### Job Queue View
- Table of pending/running/completed/failed jobs
- Filter by librarian type, status, date range
- Click into a job → see the session trace, input payload, output

### Session Tagging
Librarian sessions are tagged with `librarian_type` in their metadata so they can be filtered in the Sessions page and distinguished from dispatch and Chat With sessions.

### Nav Badge
The librarian job queue count appears in the nav alongside dispatch and other operational counts.

## Feedback Loop

The pitfall system currently has no feedback. Librarians close this loop.

### Creation
1. Agent discovers a pitfall during dispatch → writes it in experience report
2. Experience report reviewer extracts it → creates a graph note (untagged)
3. Note processor picks it up → tags it from taxonomy, deduplicates, cleans up text
4. The pitfall is now discoverable by beads sharing those tags

### Consumption
1. Bead primer pulls pitfalls by tag intersection (not FTS)
2. Agent sees relevant pitfalls in its primer
3. Agent reports in decision.json: which pitfalls it reviewed, which were useful, which were irrelevant

### Refinement
1. Experience report reviewer reads the pitfall feedback from decision.json
2. Updates relevance scores on pitfalls — pitfalls cited as useful get boosted
3. Pitfalls consistently marked irrelevant get flagged for review
4. Taxonomy maintainer may retire the tags that make irrelevant pitfalls surface
5. Over time, the pitfall library self-refines toward actually useful content

### Metrics
- Pitfall hit rate: % of dispatches where at least one pitfall was useful
- Pitfall noise rate: % of pitfalls shown that were marked irrelevant
- Note processing latency: time from creation to tagged
- Bead readiness latency: time from idea to specified
- Taxonomy staleness: % of tags with no recent notes or beads

## Dependency Chain

```
1. Dispatcher refactor (agents/dispatcher/ package structure)
2. Agent type system (auto-0lv.5) — librarian mount/lifecycle config in dispatcher
3. Taxonomy CLI + schema (graph tags infrastructure)
4. Job queue (librarian_jobs table + enqueue/dequeue)
5. Librarian registry + runner
6. Individual librarian types (parallel once infrastructure exists):
   - Experience report reviewer
   - Taxonomy maintainer
   - Note processor
   - Bead readiness pipeline
7. Feedback loop (decision.json schema + primer retrieval by tags)
8. Dashboard integration (librarian types page, job queue view)
```

Steps 1-5 are sequential infrastructure. Step 6 is parallel. Steps 7-8 are polish.

## Open Questions

1. **Concurrency controls:** Per-type max_concurrent in the registry. Taxonomy maintainer is singleton. Note processor allows parallel instances. How does the dispatcher enforce this? Check running job count before dequeue.

2. **Human override:** The CLI always works. A human can `graph note --tags pitfall,sse` directly and skip the note processor. The librarian is automation, not a gate.

3. **Librarian observability:** How does the human know if librarians are falling behind? Nav badge for queue depth. Dashboard page for job history. Alerting threshold for growing queue.

4. **Model selection:** Each type has a default (Opus). Override via registry config or per-job. The PoC runs everything on Opus (free on Max). Cost testing uses Haiku/Sonnet to find the minimum viable model per librarian type.

5. **Distributed future:** When the graph is shared across hundreds of users, librarians become a service processing contributions from all users. The job queue becomes distributed (Redis/SQS). The taxonomy becomes a shared ontology. The feedback loop aggregates across all projects. The local PoC architecture (SQLite queue, container runtime, file-based registry) should be replaceable without rewriting the librarian logic itself.
