# keyreg — the key registry

The machine-readable inventory of every key and mutation in the vault +
storage system. One YAML file records, for every key: what kind of key it is,
its custody class and the fact that forces it, how it comes to exist, the
six-field descriptor the design of record defines per key, the code that
implements it, and the formal-model references associated with it. The design
decisions behind this tool are in graph note 879ccc1e-b49; the prose design of
record is the crib sheet (graph note 1e005d5c-c11), whose section numbers the
`crib` fields cite.

## Files

- [GUIDE.md](GUIDE.md) — reading routes, field semantics, and the recovery story.
- `registry.yaml` — the source data. Edit this, not its generated views.
- `schema.json` — the canonical JSON Schema (draft 2020-12) for the data.
- `keyreg.py` — loader, validator, and query interface. It enforces the
  schema's constraints natively, so validation needs only PyYAML; when a
  jsonschema library is importable it validates against `schema.json` as
  well. Validation errors name the offending entry and field.
- `tests/` — the validator's test suite, including deliberately broken
  fixtures proving that errors are precise.
- `gen.py` — deterministic documentation and JSON generation.
- `generated/` — key register, workflow register, relationship diagram,
  model reference inventory, workflow goals (artifacts, producers, goals,
  minimal schedules), and machine-readable JSON.

## Usage

```bash
python3 tools/network/keyreg/keyreg.py validate         # exit 0 iff valid
python3 tools/network/keyreg/keyreg.py key persona_kem_private
python3 tools/network/keyreg/keyreg.py class cold        # ids by custody class
python3 tools/network/keyreg/keyreg.py edges             # derivation + seal edges
python3 tools/network/keyreg/keyreg.py reachable recovery_code
python3 tools/network/keyreg/keyreg.py plan org_sync_pull --from approval --rule admit_on_approval
python3 tools/network/keyreg/keyreg.py explain-current org_sync_pull   # current order vs the minimum
python3 tools/network/keyreg/lint.py                   # enumerable code/reference checks
python3 tools/network/keyreg/gen.py                    # regenerate committed views
agent-test run tools/network/keyreg/tests             # supported workspace test entry point
```

`reachable` follows derivation edges only; it does not evaluate decryption
of wraps or factor-policy AND/OR conditions. Authority lists in mutation
entries are descriptive, not executable policy expressions.

## Workflow prerequisites

`actors`, `artifacts` and `goals`, plus the mutation fields `actors`,
`opens` (root | persona | delegate | none), `requires` (artifact or key ids;
`all:`/`any:` nest) and `produces`, record a workflow as a monotone AND/OR
graph (first use: the org invite -> join -> admission -> sync workflow of
graph://cde6c8c6-041, bead auto-qrmlg.5). A bare per-actor artifact in a
mutation binds to the executing actor; a goal names every copy explicitly
(`<artifact>@<actor>`). An artifact's `origin` separates a copy a starting
state holds (`given`, with the `given_by` mutations that established it
before the workflow) from one a workflow mutation must produce
(`workflow`), one with no producer by a recorded defect (`defect`), and an
open question no goal requires (`open`). Designed mutations carry a `rule`.
Validation checks every reference; lint resolves artifact anchors and TLA+
proof references.

## Planning

`plan <goal> [--from <state>] [--rule <rule>]...` searches the AND/OR graph
exhaustively (Dijkstra) for the schedule with the fewest human root
windows, then the fewest steps. A root or persona step needs a window; one
actor's consecutive window steps share it; another actor's window step
closes it (a window never spans another party's ceremony). Delegate and
none steps run on a machine. Designed mutations join the search only under
their `--rule`. `explain-current` replays the goal's recorded
`current_order` and classifies each root opening: needed, mergeable into
the actor's previous window (naming what that window closed without), or
replaceable by machine steps (naming what was absent). Lint 6 fails a
consumer that precedes its producer in the current order unless the goal's
`known_defects` names it; a stale known defect fails too. The graph is
monotone, so freshness conditions (a ledger head present at adoption) are
out of its reach; tools/network/TLA/OrgAdmission.tla models them.

## Where the data comes from

The key inventory is seeded from the crib sheet's section 9 register (every
key it names has an entry), plus the keys sections 2, 18, 23, and 24 define.
Entries marked `status: designed` describe flows that exist only in the
design of record; their `code` lists are empty until the code lands.

## Deliberate exclusions

- `machine_id` — a public identifier, not key material (crib section 10).
- `PersonaKemCredential`, `CapabilityGrant`, `RosterEntry`, enrollment
  statements, descriptors, bridges — records that carry or address keys, not
  keys. Their key material appears as the relevant key entries.
- Relay/TURN session tokens and share-link grants — outside the crib
  section 9 register; add them if a mutation entry ever needs them.

## Maintenance and consumers

The inventory, lints and original generated views landed in epic auto-0ud75
(auto-q9up6 and auto-zsp1b). The Key Ceremony Atlas can consume `registry.json`;
its interactive presentation is a separate workstream (auto-74z7q).

The purpose-label lint scans Python in `tools/network` and `tools/vault`,
excluding tests, for its documented versioned-label convention. It is not a
scan of every cryptographic string in the repository. Anchor lints resolve
files and symbols; proof-reference lints resolve names, not proof results.

After a data change, regenerate and run the suite. Its staleness check compares
every committed generated artifact with a fresh generation. Record ambiguous
implementation/design distinctions in entry notes instead of inventing a
resolution. No cryptographic behavior changes merely because the map changes.
