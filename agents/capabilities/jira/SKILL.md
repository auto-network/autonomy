---
name: jira
description: Jira issue tracker (broker-backed, no credentials in the workspace). Read and search tickets, run the workspace's named queries, download attachments; post comments, set the Confirm Plan field, create tickets, upload attachments — every write pauses for operator approval.
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
jira-create payload.json                        # create a ticket
jira-attach ENTERPRISE-8385 repro.log           # upload an attachment (10MB cap)
```

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
