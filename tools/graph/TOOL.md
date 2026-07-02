# Knowledge Graph

## Purpose

Structured, graph-based, searchable knowledge system for the Autonomy Network. Ingests conversations, musings, and documents into a SQLite graph with full-text search, entity extraction, and hierarchical knowledge organization.

## Stack

- **SQLite** + FTS5 — local, sovereign, zero-dependency storage + full-text search
- **Python 3.12** — ingestion, query, CLI

## Architecture

```
schema.sql      — Database schema (sources, thoughts, derivations, entities, claims, edges, nodes)
models.py       — Dataclass models for all graph objects
db.py           — Database operations (CRUD, search, graph queries)
ingest.py       — Parsing pipeline (conversations, musings, entity extraction)
seed.py         — Knowledge hierarchy seed data (Autonomy vision structure)
cli.py          — Command-line interface
__main__.py     — Entry point for `python -m tools.graph`
```

Database: `data/graph.db`

## Usage

```bash
# Seed the knowledge hierarchy
.venv/bin/python -m tools.graph seed

# Ingest conversations and musings
.venv/bin/python -m tools.graph ingest data/chatgpt/
.venv/bin/python -m tools.graph ingest data/claude/
.venv/bin/python -m tools.graph ingest musings/

# Search
.venv/bin/python -m tools.graph search "sovereignty"

# Withdraw a note (hide from search/listings; record + links survive; still resolves via `graph read`)
graph note withdraw <src_id>

# List entities
.venv/bin/python -m tools.graph entities
.venv/bin/python -m tools.graph entities -q "autonomy"

# Find related content
.venv/bin/python -m tools.graph related "CRDT"

# Show hierarchy
.venv/bin/python -m tools.graph tree
.venv/bin/python -m tools.graph tree -v  # with descriptions

# Stats
.venv/bin/python -m tools.graph stats
```

## Data Model

| Object | Table | Description |
|--------|-------|-------------|
| Source | `sources` | Origin record (conversation, musing, document) |
| Thought | `thoughts` | User assertion/question/intent — sovereign, persistent |
| Derivation | `derivations` | AI response — linked to thought, regenerable |
| Entity | `entities` | Named concept (deduped by canonical name) |
| Claim | `claims` | Structured assertion: subject→predicate→object with provenance |
| Edge | `edges` | Typed relationship between any two objects |
| Node | `nodes` | Hierarchical knowledge tree (mission→module→component→feature) |

## Key Principles

- User thoughts are **sovereign** — persistent objects with stable IDs
- AI responses are **derivations** — tagged as regenerable, linked to the thoughts that produced them
- Entities are **deduped** by canonical name
- The hierarchy enables **agent orientation** — subtask decomposition from mission level down
- Everything is **local-first** — SQLite file you own
