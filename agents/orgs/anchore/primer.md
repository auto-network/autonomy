Shared conventions across every Anchore workspace (v5, NG, grype, syft).
These apply regardless of which repo you are editing — override only when a
workspace-level primer explicitly says so.

### Git

- **Never commit without an explicit ask.** Staging and inspecting diffs is
  fine; creating a commit is a user-authorized action. Dispatch beads are
  the exception — the dispatcher instructs you to commit your work.
- **No `Co-Authored-By: Claude` trailer** on Anchore commits. Keep
  attribution to the human author or the bead id.
- **Commit messages**: describe the *why*, not the *what*. The diff already
  shows the what. One-line subject, blank line, wrapped body.
- **Branch off the right base.** v5 patch work lives on release lines
  (e.g. `v5.27.x`), not `master`. Check the workspace primer before
  branching.

### Linting and tests

- `task lint` must succeed before you hand code back. Do not push or open a
  PR with lint failures. If a lint rule looks wrong, flag it — don't
  silently add `# noqa` / `//nolint` unless the workspace primer says so.
- Run the targeted test suite for the area you changed; the full suite
  only when the bead asks for it or your change crosses subsystems.

### Banned language

- **"Pre-existing"** is banned in commit messages, PR descriptions, bead
  titles, and code comments. Every codebase has prior state; naming
  something "pre-existing" is noise that hides whether *you* introduced
  the issue. Describe the actual state: "the `foo` endpoint returns 500
  when …", "before this change, `bar` was unused and …".

### GitHub Actions

- **Pin action versions to full commit SHAs**, not tags. Tags are mutable;
  `uses: actions/checkout@v4` can change under you. Example:
  `uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11  # v4.1.1`.
- When bumping a pinned action, record the human-readable version in a
  trailing comment so reviewers can verify intent.

### API design (M4 patterns)

These govern new REST surfaces across Anchore repos — they originated in
enterprise_ng and have since been adopted org-wide.

- **Resource-oriented URLs.** Nouns, not verbs. `/images/{digest}/scan`,
  not `/scanImage`.
- **Pagination**: cursor-based (`next_token`), not offset. Offset
  pagination breaks under concurrent writes.
- **List envelopes** carry `items`, `next_token`, and optional
  `total_count`. Never return a bare JSON array at the top level.
- **Error shape**: `{ "message": str, "detail": str|dict, "code": str }`.
  `code` is a stable machine identifier; `message` is human-readable;
  `detail` is structured context. Do not leak stack traces.
- **Time fields** are RFC 3339 strings (`created_at`, `updated_at`), UTC,
  with `Z` suffix. Never Unix epoch in public APIs.
- **Filtering**: `?filter[field]=value` rather than ad-hoc query params,
  so new filters don't collide with reserved names (`limit`, `sort`,
  `next_token`).

### API model naming

- **Request/response models** end in `Request` / `Response`
  (e.g. `CreateImageRequest`, `ScanResultResponse`). Do not reuse a
  single model for both sides of a call.
- **Resource models** are singular and carry no verb
  (`Image`, `Scan`, `Vulnerability`), never `ImageDTO` / `ImageModel` /
  `ImageEntity` — those suffixes don't survive translation between
  Python/Go/OpenAPI boundaries cleanly.
- **Enum values** are `UPPER_SNAKE_CASE` strings on the wire, regardless
  of the source language's convention. The client SDK is responsible for
  idiomatic casing on each side.
- **Nullable vs optional**: mark fields `Optional[T]` only when the server
  may omit them. `T | None` where `None` is a meaningful value is a
  distinct shape — document which is which in the schema.
