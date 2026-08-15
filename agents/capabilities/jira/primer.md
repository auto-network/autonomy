## Jira capability (issue tracker)

Broker-backed: these commands hold no Jira credential — reads run host-side;
**writes pause for operator approval** (an overlay opens on the operator's
dashboard; your command blocks until they approve or decline, then prints the
outcome or the decline).

- `jira-read KEY` — cleaned ticket JSON (description/comments as markdown)
- `jira-query --list` / `jira-query NAME [k=v …]` — this workspace's named
  queries (standardized triage/sprint views, taught per-workspace as data)
- `jira-search 'JQL'` — raw JQL search; terse rows, `--page TOKEN` to paginate
- `jira-createmeta [PROJECT [ISSUETYPE]]` — valid components/versions/priorities/severity for creation
- `jira-fields KEY [FILTER]` — read-only editable field names, ids, schema
  types, and allowed values; use before guessing custom fields or select values
- `jira-attachment ID [-o FILE]` — download an attachment (ids in `jira-read`)
- `jira-comment KEY -f body.md` — post a comment *(operator approval)*
- `jira-confirm-plan KEY -f plan.md` — set the Confirm Plan field *(operator approval)*
- `jira-update KEY --field Description -f body.md` — set rich-text or
  structured fields by display name; invalid names list all valid fields
  *(operator approval)*
- `jira-points KEY VALUE [--board ID]` — set the board's Story Points estimate,
  including when the field is absent from the issue edit screen
  *(operator approval)*
  `jira-update KEY --field 'Story Points' --value VALUE` delegates here too.
- ENTERPRISE's “Developer” requirement is the `Assignee` field.
- `jira-create payload.json` — create a ticket *(operator approval)*
- `jira-attach KEY FILE` — upload an attachment *(operator approval)*
- `jira-change-type KEY 'Bug'` — change the issue type (Jira's "Move");
  `--list` shows the project's types. Preflights invalid/no-op/sub-task
  targets before staging *(operator approval)*
- `jira-transition KEY 'Name' [--field 'Name=value' …]` — move through a
  workflow transition; `--list` shows what's valid now + required fields.
  Preflight fails with the missing-fields list before staging approval
  *(operator approval)*

`jira-transition --list` can only show required fields exposed on Jira's
transition screen. A workflow validator may name an off-screen field only when
execution fails. In that case, run `jira-fields KEY [FILTER]`, set the exact
editable field with `jira-update`, and retry. `jira-update` now validates field
names through this read-only surface before it opens an approval.

**Which field for what:** the **Confirm Plan** custom field holds step-by-step
QA instructions to reproduce the bug and prove the fix — command-by-command
(`anchorectl`, `curl`, `psql`, …) with expected results. **Comments** hold
narrative: findings, discussion, corrections. Don't dump repro steps into a
comment — put them in Confirm Plan via `jira-confirm-plan`.

Markdown in bodies is converted to Jira's document format host-side. A
markdown image alone on its line whose target names one of the ticket's
attachments renders inline (attach first with `jira-attach`); anything
unresolvable stays literal text. For full usage see
`agents/capabilities/jira/SKILL.md`.
