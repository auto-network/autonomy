"""Public-surface curation: librarian tooling for publication_state promotion.

Implements the Bootstrap Allowlist charter (graph://93cf3026-1df):

- ``allowlist``  — YAML loader + validator for tier lists.
- ``audit``      — enumerates candidate notes in an org DB, reports current tags /
  last_updated / pending_comments_count / proposed_state for operator review.
- ``promote``    — reads a committed allowlist and applies the state transitions,
  recording an audit note that captures the session's provenance.

The bundled ``autonomy-bootstrap-allowlist.yaml`` is the one-shot v1 allowlist
for the autonomy org's own DB; peer orgs add their own under the same naming
pattern as they come online.
"""
