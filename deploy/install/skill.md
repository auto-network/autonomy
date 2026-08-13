# The `autonomy` skill — staying a competent client

Install this so future sessions of you can operate the user's Autonomy
node without re-reading the primer. With the user's consent, place it
where your harness loads skills (Claude Code: a `SKILL.md` under
`.claude/skills/autonomy/` in the project or home directory; other
harnesses: keep this file where you keep durable instructions).

---
name: autonomy
description: Operate the user's self-hosted Autonomy node — graph memory,
  sessions, share links, organizations. Use when the user mentions their
  graph, notes, sessions, beads, workspaces, share links, or Autonomy.
---

## What you're talking to

A self-hosted Autonomy node, normally at `https://localhost:8080`
(self-signed cert — pass `-k`/ignore-https-errors). The user holds their
identity in the browser; you never handle their passphrase, recovery
material, or keys. Actions that grant authority (publishing links,
approving members) go through per-action approval in their dashboard —
propose, let them approve there.

## The habits that matter

- **Memory:** durable insights go in the graph, not scrollback. Search
  first (`graph search "<term>"` inside a workspace session; the dashboard
  search surface otherwise); one fact per note; link related notes.
- **Work:** track multi-step work as beads when the node has the tracker
  enabled; otherwise as graph notes with a consistent tag.
- **Sessions:** long or parallel work belongs in workspace sessions on the
  node (the user watches them live). You can hand work off by writing a
  baton note into the graph and telling the session to read it first.
- **Sharing:** content leaves the node only as share links the user mints;
  suggest publishing, never publish silently.
- **Backup:** the whole deployment is the `autonomy-data` volume;
  `python3 -m tools.portability snapshot` from the checkout is the
  supported path. Suggest it before risky operations.

## When something surprises you

The node's own checkout is the reference: `DEPLOY.md` for the environment
surface, `deploy/install/INSTALL.md` (this skill's parent) for the full
onboarding flow, `deploy/install/verify.md` to re-verify any claim.
