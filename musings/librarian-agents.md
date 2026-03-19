# Librarian Agents — Design Document

## Overview

Librarians are lightweight agents that maintain knowledge quality through classification, linking, and lifecycle management. They don't write code — they read, tag, link, and organize. They are the custodians of the knowledge graph, the bead backlog, and the operational feedback loop.

## Why Librarians Exist

The Autonomy platform generates knowledge artifacts at high velocity: beads are created by agents and humans, notes are dropped during conversations, experience reports are written after every dispatch, sessions accumulate in the graph. Without curation, this becomes a junk drawer — duplicates pile up, pitfalls go untagged, beads sit at `readiness:idea` forever, experience reports are written and never read.

Today the human is the librarian. This doesn't scale. When 10 agents dispatch per day, each producing an experience report, discovered beads, and session logs, the human can't review everything. The knowledge degrades — primers surface irrelevant pitfalls via loose FTS matching, beads lack file paths and acceptance criteria, duplicate work gets dispatched.

Librarians close the feedback loop. They process raw output into curated knowledge that makes the next dispatch better.

## Librarian Types

### Experience Report Reviewer

**Trigger:** dispatch:done event (job enqueued when dispatcher collects results)

**Input:** Experience report, decision.json, session JSONL, bead description

**What it does:**
- Reads the experience report for tooling bugs, pitfalls, and tool feedback
- Extracts pitfalls → creates graph notes (untagged — the note librarian will process them)
- Extracts tooling bugs → creates beads at readiness:idea
- Extracts discovered work not captured in decision.json → creates beads
- Reports which primer pitfalls were useful vs irrelevant (from decision.json pitfalls_reviewed/pitfalls_useful/pitfalls_irrelevant fields)
- Detects recurring patterns across multiple reports → escalates priority

**Output:** New notes, new beads, pitfall feedback scores

### Taxonomy Maintainer

**Trigger:** Scheduled (daily), or on-demand when significant code changes land

**Input:** Current taxonomy, repo file tree, bead epic structure, existing tags in use

**What it does:**
- Scans bead epics and their children for subsystem names
- Scans file paths mentioned in bead descriptions
- Scans the actual directory/file structure of the repo
- Scans existing tags in use across notes and beads
- Adds new tags when new subsystems emerge
- Retires tags when code is deleted or refactored
- Validates parent-child tag hierarchy matches the codebase structure
- Publishes the updated canonical tag list

**Output:** Updated taxonomy in graph.db tags table

### Note Processor

**Trigger:** New note created (job enqueued by `graph note` or by other librarians creating notes)

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

### Bead Readiness Librarian

**Trigger:** New bead at readiness:idea (job enqueued on bead creation)

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
3. If `sse` is not in the taxonomy, the note is still created but a warning is printed and a job is enqueued for the note librarian
4. Bead primer builds: bead has labels `tag:sse,tag:dashboard` → primer queries `source_tags` for notes with matching tags → relevant pitfalls surface
5. Taxonomy librarian periodically validates that tags match the codebase

### Primer Retrieval (replacing FTS)

Current (broken):
```python
# OR-match title words against all pitfall notes
fts_q = _sanitize_fts_query(" ".join(title_words[:5]), or_mode=True)
```

Future:
```python
# Get bead's tags from Dolt labels
bead_tags = [l.replace("tag:", "") for l in bead["labels"] if l.startswith("tag:")]
# Find notes sharing any tag
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

## Job Queue

Librarians are triggered via a job queue in SQLite. Events (dispatch:done, note created, bead created) enqueue jobs. Scheduled jobs are enqueued by a timer check each dispatcher cycle.

### Schema (dispatch.db or new librarian.db)

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
    session_id TEXT               -- links to the graph session for this run
);
```

### Enqueueing

```python
# In dispatcher, after collecting results:
from agents.dispatcher.jobs.queue import enqueue
enqueue("review_report", payload={
    "bead_id": agent.bead_id,
    "report_path": f"{agent.output_dir}/experience_report.md",
    "decision_path": f"{agent.output_dir}/decision.json",
    "run_id": run_id,
})

# In graph note command, after creating a note:
enqueue("process_note", payload={"source_id": new_note_id})

# In graph bead / bd create, after creating a bead:
enqueue("readiness_triage", payload={"bead_id": new_bead_id})
```

### Scheduling

Each dispatcher cycle checks:
```python
# Has it been 24 hours since the last taxonomy refresh?
last_run = get_last_completed("refresh_taxonomy")
if not last_run or (now - last_run) > timedelta(hours=24):
    enqueue("refresh_taxonomy")
```

## Agent Lifecycle

Librarians run as agents in containers but with a different lifecycle than implementation agents.

### Permissions
- Repo: read-only mount
- Beads (bd): read-write (create beads, update status, set labels)
- Graph: read-write (create notes, add tags, link sources)
- No worktree, no git branch, no merge

### Dispatch Flow
1. Dispatcher picks up a librarian job from the queue
2. Looks up the librarian type in the registry
3. Builds the prompt: static role prompt + dynamic task primer from payload
4. Launches container with librarian permissions (repo ro, bd rw, graph rw)
5. Polls for completion (same as implementation agents)
6. Collects results — no merge step, no worktree cleanup
7. Updates job status in the queue
8. Ingests session into graph

### Registry

```
agents/librarians/
    registry.py                  — type definitions, primer routing
    experience_reviewer/
        prompt.md                — static role prompt
        primer.py                — builds task context from payload
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

Each type is registered with:
```python
@librarian_type(
    name="experience_reviewer",
    description="Reviews dispatch experience reports for pitfalls, bugs, and feedback",
    trigger="dispatch:done",
    schedule=None,
    priority=1,
)
```

### Adding a New Librarian Type
1. Create a directory under `agents/librarians/`
2. Write `prompt.md` — the static role definition
3. Write `primer.py` — a function that takes a job payload and returns primer text
4. Register with the `@librarian_type` decorator
5. The scheduler/trigger system picks it up automatically

## Dashboard Integration

### Librarian Types Page
- List of all registered librarian types
- For each: name, description, trigger type, schedule, last run time, success rate
- Click into a type → see its static prompt, recent sessions, job history

### Job Queue View
- Table of pending/running/completed/failed jobs
- Filter by librarian type, status, date range
- Click into a job → see the session trace, input payload, output

### Session Tagging
Librarian sessions are tagged with `librarian_type` in their metadata so they can be filtered in the Sessions page:
- "Show only experience reviewer sessions"
- "Show only taxonomy maintenance runs"

### Nav Badge
The librarian job queue count could appear in the nav — "3 pending jobs" — similar to how dispatch shows running agents.

## Feedback Loop

The pitfall system currently has no feedback. Librarians close this loop:

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
2. Agent type system (auto-0lv.5) — librarian mount/lifecycle config
3. Taxonomy CLI + schema (graph tags infrastructure)
4. Job queue (librarian_jobs table + enqueue/dequeue)
5. Librarian registry + launcher
6. Individual librarian types (can be developed in parallel):
   - Experience report reviewer
   - Taxonomy maintainer
   - Note processor
   - Bead readiness pipeline
7. Dashboard integration (librarian types page, job queue view)
8. Feedback loop (decision.json schema + primer retrieval update)
```

Steps 1-5 are sequential infrastructure. Step 6 is parallel — each librarian type is independent once the infrastructure exists. Steps 7-8 are polish that can happen alongside or after step 6.

## Open Questions

1. **Concurrency:** Can multiple librarians of the same type run simultaneously? The note processor might get 5 notes at once. Running 5 instances is fine if they don't conflict — each processes a different note. But the taxonomy maintainer should be singleton.

2. **Failure handling:** If a librarian fails (bad note text, graph DB locked, etc.), the job stays in the queue. How many retries? Does a failed note processor job block other note processing?

3. **Human override:** Can the human skip the librarian and tag/promote notes directly? Yes — the CLI should always work. The librarian is automation, not a gate.

4. **Librarian observability:** How does the human know if librarians are falling behind? The nav badge helps, but we may need alerting for a growing queue.

5. **Cost:** Librarians consume API tokens. The experience report reviewer runs after every dispatch — that's potentially 10+ runs per day. Each is lightweight (classification, not coding), so token cost is low, but it adds up. Haiku-tier models may be sufficient for most librarian work.
