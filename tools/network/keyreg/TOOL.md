# keyreg — the key registry

The machine-readable inventory of every key and mutation in the vault +
storage system. One YAML file records, for every key: what kind of key it is,
its custody class and the fact that forces it, how it comes to exist, the
six-field descriptor the design of record defines per key, the code that
implements it, and the machine-checked proofs that cover it. The design
decisions behind this tool are in graph note 879ccc1e-b49; the prose design of
record is the crib sheet (graph note 1e005d5c-c11), whose section numbers the
`crib` fields cite.

## Files

- `registry.yaml` — the data. The only file humans edit.
- `schema.json` — the canonical JSON Schema (draft 2020-12) for the data.
- `keyreg.py` — loader, validator, and query interface. It enforces the
  schema's constraints natively, so validation needs only PyYAML; when a
  jsonschema library is importable it validates against `schema.json` as
  well. Validation errors name the offending entry and field.
- `tests/` — the validator's test suite, including deliberately broken
  fixtures proving that errors are precise.

## Usage

```bash
python3 tools/network/keyreg/keyreg.py validate         # exit 0 iff valid
python3 tools/network/keyreg/keyreg.py key persona_kem_private
python3 tools/network/keyreg/keyreg.py class cold        # ids by custody class
python3 tools/network/keyreg/keyreg.py edges             # derivation + seal edges
python3 tools/network/keyreg/keyreg.py reachable recovery_code
```

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

## Downstream (other beads in epic auto-0ud75)

- The mutation inventory and the code-completeness checks that fail a merge
  when a fold handler or purpose label is missing from the registry:
  bead auto-q9up6.
- The generated views (key-relationship diagram, per-key register,
  proof-coverage table, registry.json): bead auto-zsp1b.
