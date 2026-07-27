---
name: jira
description: Jira issue tracker (broker-backed, no credentials in the workspace). Read/search tickets; comment; update rich-text and structured fields such as Fix Version and Assignee; create tickets and upload attachments. Every write pauses for operator approval.
---

# Jira capability — agent skill

Implementation: `autonomy/jira` · Contract: `issue_tracker@1` · Delivery:
`mounted_tools` (the `jira-*` commands below are on PATH).

## Security model — why writes pause

These tools hold **no Jira credential**. Reads call a dashboard broker route
and the Jira API call runs host-side. Writes are staged as operator-approval
requests: an overlay with your exact content opens on the operator's
dashboard, your command **blocks** until they decide, and the write executes
host-side only after an approval. A decline exits non-zero with a message —
confirm intent with the user before retrying; don't loop on a declined write.
What the operator approves is exactly what is written; nothing is edited
in-flight.

## Commands

### Reading

```bash
jira-read ENTERPRISE-8385          # cleaned ticket: summary, status, description,
                                   # comments, attachments — ADF already markdown
jira-createmeta                    # ENTERPRISE/Bug creation metadata (defaults)
jira-createmeta PROJ Story         # any project/issuetype
jira-attachment 12345              # download an attachment by id (ids are in
                                   # jira-read's attachments list); -o FILE to name it
```

### Searching

```bash
jira-query --list                  # this workspace's named queries (name, params, summary)
jira-query mine                    # run one; params as key=value
jira-query release version=6.1.0
jira-search 'project = ENTERPRISE AND status = "Pending RC"'   # raw JQL escape hatch
```

Named queries are the standardized sprint-planning views this workspace has
been taught (they live as data on the workspace's capability-enable Setting —
the broker resolves your session to its workspace, so the set you see is
always your workspace's own). Prefer them over hand-rolled JQL; reach for
`jira-search` when no named query covers the question. Both return terse rows
(`key`, `summary`, `status`, `assignee`, `fix_versions`, `sprint`,
`story_points`, `updated`) — follow up with `jira-read KEY` for detail.
Results page by opaque token: pass a returned `next_page_token` back via
`--page TOKEN`; `--max N` caps the page size (default 50).

### Writing (each pauses for operator approval)

```bash
jira-comment ENTERPRISE-8385 -f findings.md     # or: echo "..." | jira-comment KEY
jira-confirm-plan ENTERPRISE-8385 -f plan.md    # sets the Confirm Plan field
jira-update ENTERPRISE-8385 --field Description -f body.md   # rich text or structured value
jira-update ENTERPRISE-8385 --field 'Fix versions' -f version.txt
jira-update ENTERPRISE-8385 --field Assignee -f assignee.txt
jira-create payload.json                        # create a ticket
jira-attach ENTERPRISE-8385 repro.log           # upload an attachment (10MB cap)
jira-transition ENTERPRISE-8385 'Code Review'   # move through a workflow transition
jira-change-type ENTERPRISE-8853 Bug            # change the issue type (Jira's "Move")
```

`jira-update` sets rich-text and supported structured fields by display name
(or a literal `customfield_NNNNN` id). The field id and schema are discovered
from the ticket's editmeta host-side, so names are portable. Rich-text bodies
(`Description`, `Confirm Plan`, textarea custom fields) are markdown converted
to ADF. Structured values are coerced from file/stdin text: comma-separated
versions/components become arrays, option values become Jira option objects,
and user display names or emails resolve to an `accountId`. For example:

```bash
printf '%s\n' 'Enterprise 6.2.0' |
  jira-update ENTERPRISE-8853 --field 'Fix versions'
printf '%s\n' 'Jeremy Spilman' |
  jira-update ENTERPRISE-8853 --field Assignee
```

Use Jira's editable field name. Invalid names fail with the complete valid-field
list from Jira. In ENTERPRISE, the road-to-RC “Developer” requirement is the
`Assignee` field.

`jira-confirm-plan` remains the idiomatic shortcut for Confirm Plan.

### Changing the issue type

An issue-type change is Jira's "Move", not a field edit — `jira-update
--field 'Issue Type'` is rejected before it reaches Jira. Use
`jira-change-type KEY 'Bug'`: it resolves the target name to the project's
numeric type id host-side (the edit endpoint 400s on name strings),
preflights before staging approval — unknown types fail with the valid
list, no-ops and sub-task conversions (which the REST API can't do) fail
with a clear message — and `jira-change-type KEY --list` shows the
project's types with the current one marked. Typical use: reclassifying a
Task as a Bug so it can carry a Confirm Plan (a bug-workflow field
enforced by the Pending-RC validator).

### Inline images

In `jira-comment`, `jira-confirm-plan`, and `jira-update` bodies, a markdown
image **alone on its line** whose target names an existing attachment of the
same ticket renders inline at that spot:

```bash
jira-attach ENTERPRISE-8385 failure-screenshot.png     # first: attach (approval)
cat > body.md <<'EOF'
The dialog renders behind the viewer:

![failure](failure-screenshot.png)
EOF
jira-update ENTERPRISE-8385 --field Description -f body.md
```

Resolution happens host-side at execution time by filename. An image whose
target matches no attachment (or an image in the middle of a sentence) stays
as literal text — the write never fails over an image. Attach files first;
duplicate filenames resolve to the newest upload.

### Transitions

`jira-transition KEY --list` (read, no approval) shows the transitions valid
from the ticket's current status, each with its required fields and whether
the ticket already satisfies them. The write matches your name against the
transition name *or* the destination status, case-insensitively.

Transitions can carry required-field validators. `jira-transition`
preflights them: if a required field is empty on the ticket and not
supplied, it fails with the missing list **before** anything is staged for
approval — supply values inline with repeatable `--field 'Name=value'`
(comma-separate multi-value fields; users by exact display name or email):

```bash
jira-transition ENTERPRISE-8385 'Pending RC' \
  --field 'Fix versions=Enterprise 6.1.0' --field 'Assignee=Jane Doe'
```

Some validators (e.g. "Confirm Plan must be populated") have no transition
screen field — those surface as a clear Jira error through the approval
result; fix the ticket (e.g. `jira-confirm-plan`) and retry.

`jira-create` payload — the Jira fields object (bare or under `"fields"`);
a plain-string `description` may be markdown (converted host-side):

```json
{"fields": {
  "project": {"key": "ENTERPRISE"},
  "issuetype": {"name": "Bug"},
  "summary": "…",
  "description": "markdown here",
  "components": [{"id": "…"}],
  "versions": [{"id": "…"}]
}}
```

Use `jira-createmeta` first — it returns the valid component/version/priority/
severity ids and the latest released version.

## Ticket schema — which field holds what

- **Confirm Plan** (custom field): step-by-step QA instructions to reproduce
  the bug and then prove the fix. Command-by-command with expected results;
  common tools: `anchorectl`, `curl`, `psql`. Write it with
  `jira-confirm-plan`, never as a comment.
- **Comments**: narrative — root-cause findings, discussion, corrections,
  status. Write with `jira-comment`.
- **Description**: the bug/story statement itself (usually authored at
  creation).

All bodies are markdown; conversion to Jira's ADF happens host-side,
including inside the Confirm Plan field.

## Probe

`jira-read --probe` checks broker reachability + host-side auth (used as the
capability probe).
