# Jira capability — agent skill (placeholder)

Stable stub so future beads can target `agents/capabilities/jira/SKILL.md`
without inventing the path. Not yet a real Agent Skills bundle.

The full skill content is added by the Jira capability MVP bead (see
graph://86e04207-a25 § Phase 3 — *Jira capability MVP*).

## What this capability will provide

Implementation: `autonomy/jira`.

Contract implemented:

- `issue_tracker@1`

Delivery mode: `mounted_tools` — the tool bundle in `tools/` is mounted
into the workspace and stable command symlinks (`jira-read`,
`jira-comment`, `jira-create`, `jira-createmeta`) appear on PATH.

## Intended agent surface

The agent will be told it can:

- `jira-read KEY`
- `jira-comment KEY -f body.md`
- `jira-create payload.json`
- `jira-createmeta`

Auth (`JIRA_EMAIL`, `JIRA_BASE_URL`, token at `/run/secrets/jira_token`)
is pre-injected. Markdown-to-ADF caveats are documented when the MVP
bead lands.
