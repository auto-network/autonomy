## Jira capability (issue tracker)

Broker-backed: these commands hold no Jira credential — reads run host-side;
**writes pause for operator approval** (an overlay opens on the operator's
dashboard; your command blocks until they approve or decline, then prints the
outcome or the decline).

- `jira-read KEY` — cleaned ticket JSON (description/comments as markdown)
- `jira-createmeta [PROJECT [ISSUETYPE]]` — valid components/versions/priorities/severity for creation
- `jira-attachment ID [-o FILE]` — download an attachment (ids in `jira-read`)
- `jira-comment KEY -f body.md` — post a comment *(operator approval)*
- `jira-confirm-plan KEY -f plan.md` — set the Confirm Plan field *(operator approval)*
- `jira-create payload.json` — create a ticket *(operator approval)*
- `jira-attach KEY FILE` — upload an attachment *(operator approval)*

**Which field for what:** the **Confirm Plan** custom field holds step-by-step
QA instructions to reproduce the bug and prove the fix — command-by-command
(`anchorectl`, `curl`, `psql`, …) with expected results. **Comments** hold
narrative: findings, discussion, corrections. Don't dump repro steps into a
comment — put them in Confirm Plan via `jira-confirm-plan`.

Markdown in bodies is converted to Jira's document format host-side. For full
usage see `agents/capabilities/jira/SKILL.md`.
