# Cleansing client content from the public repository

`auto-network/autonomy` on GitHub is public. Everything committed here is
published, including every commit message and every file ever added, until
the history that carries it is rewritten and the old objects are gone from
every copy. This runbook records the policy, the one cleanse performed on
2026-10-01, and the exact steps that remove the content from history and
from every published copy. It is written so that it never has to be
reconstructed again.

## The rule

A client organization may be named. "Anchore" as a noun, as an org slug in
a test fixture, or as the subject of a sentence is fine. Nothing beyond the
noun belongs here: no ticket keys or Jira project names, no internal
documents or product plans, no product or repository names, no employee
names, no persona labels, registry UUIDs, identity-provider tenants, serving
hostnames with real organization-specific data behind them, no build or
environment variable names that belong to the client's product, and no
exported artifacts (mission pages, pillar sites, attachments) under any
pretext, including "the harness needs a real document". The same rule
covers the operator's own infrastructure: tailnet names and machine
hostnames are not fixture material even when certificate transparency has
already published them.

The gate is the review before a push: `gitleaks` for secrets, then an
identifier sweep of every added line in the range for organization names,
hostnames, tailnet suffixes, persona labels, UUIDs, ticket keys, and
people's names, with each hit judged real or synthetic. A synthetic value
that looks like a real one is fine; a real value in a test is a leak.

## What the 2026-10-01 commit removed

Range reviewed: `effd6489..5366c6a8` (378 unpushed commits) plus the whole
tree at the tip, because the rule applies to the tree, not only to the
range.

- `tools/network/registry/tests/manual/pillar-sample.html`: a complete
  client project briefing exported from Mission Control and committed on
  2026-08-11 (`dc87de27`) as a WebKit compose fixture. Deleted; the path is
  now gitignored and the harness README says to export a document locally.
- The client's Jira project key, ticket keys, and product version names in
  the Jira capability, its tools, and test fixtures: replaced with `PROJ`,
  `PROJ-NNNN`, and a fictional product name.
- Client repository, product, product-CLI, and product-feature names used as
  fixture ids, workspace ids, mission names, and examples (nine distinct
  names across about a hundred files): replaced with fictional names.
- The client's registry organization UUID, persona label, Okta tenant, and
  the operator's persona label: replaced with synthetic values of the same
  shape.
- Three real routing rows for client repositories in
  `tools/graph/ingest.py`: removed. A deployment that needs them sets
  `AUTONOMY_HOST_PROJECT_ORGS`.
- Ten client workspace rows in the credential-homing audit baseline
  (`tools/graph/audit_credential_homing.py`): removed. A deployment keeps
  them in a JSON file named by `AUTONOMY_CREDENTIAL_HOMING_BASELINE`.
- A hardcoded client org fallback in the worktrees page, client-specific
  prose in deploy docs, TLA provenance, and skill text: removed or
  generalized.
- The operator's tailnet hostname in `deploy/VOICE.md` and three test
  files: replaced with the synthetic `tail1234.ts.net` form.

Operator action after deploying this commit, on every node that routed
host sessions for client repositories or ran the homing audit:

```bash
# host-session routing, formerly hardcoded in tools/graph/ingest.py
export AUTONOMY_HOST_PROJECT_ORGS='{"/home/<user>/workspace/<repo>": "<org>"}'
# credential-homing baseline, formerly hardcoded in audit_credential_homing.py
export AUTONOMY_CREDENTIAL_HOMING_BASELINE=/path/outside/the/repo/baseline.json
# baseline.json: [["autonomy.workspace", "<org>/<workspace>", "env.GH_TOKEN"], ...]
```

## Removing it from history and from every copy

The tree is clean after the commit above, but the content stays retrievable
from history until the history is rewritten and every copy of the old
objects is replaced. Do these in order. Each step depends on the one before.

### 1. Rewrite history on the source of truth (Home)

Work on a fresh clone so a mistake costs nothing.

```bash
git clone --no-local /path/to/home/repo /tmp/rewrite && cd /tmp/rewrite
pip install git-filter-repo        # or the distribution package

# Drop the exported document from every commit that ever carried it.
git filter-repo --invert-paths \
  --path tools/network/registry/tests/manual/pillar-sample.html

# Reword commit messages. One file per original commit sha holds the
# replacement body; messages not listed pass through unchanged.
git filter-repo --commit-callback '
import os
d = "/path/to/messages"
h = commit.original_id.decode()
for name in os.listdir(d):
    if name.endswith(".new.txt") and h.startswith(name[:-8]):
        commit.message = open(os.path.join(d, name), "rb").read()
'
```

For the 2026-10-01 cleanse the message set is one commit, `51749106`,
whose body named the operator's persona label and root-key suffix. The
replacement is in the review output directory of session auto-0930-202034
(`review/messages/51749106.new.txt`). The 111 already-public commit
messages that name the client as a noun are within the rule and are left
alone.

Then verify before anything leaves the machine:

```bash
git log --all --format=%H -- tools/network/registry/tests/manual/pillar-sample.html | wc -l   # 0
git log --all --format=%B | grep -c '<the persona label>\|<the root suffix>'                     # 0
git rev-list --count master                                                                     # unchanged
```

### 2. Replace master on GitHub

The repository has no forks, stars, or watchers (checked 2026-10-01), so
nothing downstream on GitHub holds the old objects.

```bash
git push --force origin master
# every other branch and tag must point at rewritten commits or be deleted:
git ls-remote origin | grep -v refs/heads/master
```

Old commits stay fetchable by hash and visible in cached views until GitHub
purges them. Open a support request asking for garbage collection and cache
clearing of the unreachable objects, naming the repository and the date of
the force-push. Pull requests that reference old hashes must be closed and
deleted before the request, or they keep the objects reachable.

### 3. Rebuild and replace the published images

`deploy/publish-images.sh` clones the tree into the node image, so every
published release carries the working copy of the tree at its commit. The
2026.09.26 release (`deploy/releases/2026.09.26-0d46057.env`) contains the
deleted document.

```bash
# from the rewritten checkout, after step 2
deploy/publish-images.sh            # new images, new lock file under deploy/releases/
```

Then delete every older package version of `autonomy-node`,
`autonomy-session`, `autonomy-session-platform`, and `autonomy-session-dind`
on GHCR (organization packages page, or the packages API). Commit the new
lock file; do not edit an old one.

### 4. Re-base every clone that has fetched from GitHub

Each must be a fresh clone or a hard reset to the rewritten master. A
fetch-and-merge re-introduces the old commits and the next sync pushes them
back.

- Home's working checkout (if not the clone used in step 1).
- Every node's managed clone and checkout: `graph worktree sync` refuses a
  dirty tree, so land or discard session work first, then on the node:

```bash
docker exec -u autonomy <node>-dashboard-1 sh -c \
  'cd /app && git fetch origin && git reset --hard origin/master'
```

  and reset the managed clone under `data/repos/` the same way. Session
  worktrees branch from the managed clone; end live sessions before the
  reset or their branches keep the old objects alive locally.
- Any machine that ran `deploy/quickstart.sh`, which clones from GitHub.

Local reflogs and packfiles still hold the old objects until expired:

```bash
git reflog expire --expire=now --all && git gc --prune=now --aggressive
```

### 5. What cannot be recalled

Anyone who cloned anonymously between the first public push of the content
and the force-push has it. The Wayback Machine had no capture of the
repository and Software Heritage had not archived it as of 2026-10-01;
check both again at the time of the rewrite:

```bash
curl -s 'http://archive.org/wayback/available?url=github.com/auto-network/autonomy*'
curl -s -o /dev/null -w '%{http_code}\n' \
  https://archive.softwareheritage.org/api/1/origin/https://github.com/auto-network/autonomy/get/
```

A capture means a takedown request to that archive. Certificate transparency
logs keep every hostname a certificate was ever issued for; nothing removes
those, which is why hostnames must not be committed in the first place.

## Keeping it clean

Run the pre-push review on every range before it goes to origin. The
reviewer's checklist and the identifier sweep used on 2026-10-01 are in
session auto-0930-202034's review output directory (`review/CHECKLIST.md`),
and the sweep is three commands:

```bash
gitleaks git --log-opts="origin/master..HEAD" --no-banner
git diff -U0 origin/master HEAD | grep '^+' | grep -niE \
  'ts\.net|persona-[0-9a-f]{20}|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-|[A-Z]{3,}-[0-9]{3,}|\.okta\.com|atlassian\.net'
git log --format=%B origin/master..HEAD | grep -niE 'persona-|suffix|okta|atlassian'
```

Every hit is a decision, recorded with the commit and path, before the push.
