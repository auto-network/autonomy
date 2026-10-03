# Deploying Autonomy — environment surface

This repository is public. What may and may not be committed about client
organizations, and how content is removed from history and from every
published copy, is in `deploy/PUBLIC-REPO-CLEANSE.md`.

## First-run initialization

A fresh checkout becomes a working **empty** deployment with one command
(idempotent — safe to re-run any time; see `tools/init/TOOL.md`):

```bash
python -m tools.init --org myorg --org-name "My Org"
./tools/dashboard/start-dashboard.sh
```

This creates the data dirs, an empty schema'd `data/graph.db`, per-org DBs
(`personal` + your named first org) with identity Settings, the dashboard
operational DBs, the bootstrap public-surface allowlist Setting, and a
self-signed TLS keypair at `data/tls.crt`/`tls.key` (picked up automatically
by `start-dashboard.sh`; for browser-trusted certs use
`tools/dashboard/renew-tls-cert.sh` on a tailnet, or terminate TLS in a
reverse proxy / tunnel with Let's Encrypt). No seeded content is assumed or
required — search and list surfaces start out empty.

The dashboard's own startup runs the same org bootstrap, honoring
`AUTONOMY_FIRST_ORG` / `AUTONOMY_FIRST_ORG_NAME`, so exporting those before
first launch is equivalent to passing `--org`/`--org-name`.

## One root for the whole volume

`AUTONOMY_DATA_ROOT` names the base directory every persistent store
resolves under (`<root>/graph.db`, `<root>/orgs/<org>.db`, …). It is a
base directory, never a single-file pin: per-org routing composes through
it, so it cannot recreate the org-collapsing failure a whole-database
`GRAPH_DB` pin causes. Precedence everywhere: a store's own variable →
`AUTONOMY_DATA_ROOT` → the caller's explicit root → the repository-local
default (refused under `AUTONOMY_REFUSE_REAL_DATA_FALLBACK`). Relative
values are refused. An ambient-rooted deployment never falls back to the
repository-relative legacy database. The node containers the workflow
simulation generates set it to `/app/data`.

## Sovereign distribution (Docker Compose)

The container path packages the first-run story above into one command:

```bash
git clone <source you chose> autonomy && cd autonomy
AUTONOMY_FIRST_ORG=myorg docker compose up -d
# → https://localhost:8080  (self-signed cert; accept once)
```

**Host prerequisites:** Docker with Compose, nothing else. The node runs its
own tmux server (the `tmux` service owns every session pane, so sessions
survive a dashboard restart), and the operator's host terminal is a session
container on the `autonomy-host-terminal` image with the node's volumes, the
Docker socket, and the operator home read-only at `/host-home`. No tmux,
Claude Code or other session tool is installed on the host.

**First host terminal imports your existing sign-in.** When you open a host
terminal and the node holds no Claude account yet, the dashboard runs
`graph credentials import --home /host-home` once, with no prompt: it reads
`~/.claude/.credentials.json`, `~/.codex/auth.json` and the Grok sign-in from
your home (read-only; the files are never modified) and seals what it finds
into your personal credential store, so the terminal and every later session
can run inference immediately (decision graph://89d3c8df-544, S3). Nothing is
imported while an account is already installed. With nothing to import, the
request fails with a message naming `/host-home/.claude/.credentials.json`.
To keep your home's sign-ins out of the node, leave `AUTONOMY_HOST_HOME`
pointing at a directory without them and install accounts from the
dashboard instead.

**Sovereign means:** the image builds from this checkout
(`deploy/Dockerfile`); no account, token, or login to any registry is
required; there is no license check and no phone-home. The only external
fetches are anonymous and happen at **build time** — the base image
(`python:3.12-slim`), PyPI wheels (`deploy/requirements.txt`), and the
tailwind binary — and each is overridable to mirrors you control via
compose env (`AUTONOMY_BASE_IMAGE`, `AUTONOMY_TAILWIND_URL`). At
**runtime** the deployment is fully self-contained: every UI library the
dashboard serves is vendored in the image
(`tools/dashboard/static/vendor/`, see `VENDOR.md` there), the CSP names
no third-party origin, and browsers never contact a CDN — a fresh
install works offline. Pinned by
`tools/dashboard/tests/test_no_cdn_dependencies.py`. Distribution is a
`git clone` you build from, or an image you push to a registry *you* choose —
never a mandated one. (Building from source is always from a git clone: the
image stamps `/app/VERSION` by reading the commit from `.git` at build, so a
detached source tarball with no `.git` is not a build input — clone instead.)

### The Docker socket — required for a working node

The compose file bind-mounts `/var/run/docker.sock` into the dashboard, and it
is **required**, not an optional hardening choice. The node launches every agent
session as a **host-level sibling container** through that socket, never as a
child of a node-owned daemon. The reason is the session topology: a session
image can run its own nested Docker daemon (`agents/Dockerfile.dind`, for
large product compose stacks), and that nested daemon
falls back to the `vfs` storage driver because OverlayFS is not reliable inside
a container. If the node were *also* a nested daemon, the session would sit two
levels deep and its own daemon three — overlay-on-overlay, which does not work.
Host-socket-at-node keeps every session a flat host-daemon sibling, one nesting
level each, which is why it works. (Design of record: `graph://a284bdea`.)

The socket is effective host-root. A node that will deliberately never launch
sessions or hold secrets can remove the mount, but it is then a viewer, not a
working node — session launch (`docker run`), topology discovery
(`docker inspect`), and the in-memory secret-store provisioning
(`agents/secret_ramfs.py`) all reach the daemon through it.

### Local Service gateway

The optional `service-gateway` profile is the node-local TLS terminator for
sovereign Service publications. It builds from the digest-pinned ordinary Caddy
image (no `caddy-l4` module). The one-line derivative removes upstream's
privileged-port file capability because this gateway listens only on
unprivileged 9443; the binary and modules remain the official distribution. It
joins only the Compose default network and publishes no host port in the
production file. The profile is deliberately inactive until the dashboard's
gateway supervisor needs it. Prepare its image during installation without
starting a container:

```bash
docker compose --profile service-gateway build service-gateway
```

After that, publication state owns the container. The Dashboard starts the
exact `autonomy` Compose service, atomically loads the complete desired config,
recovers it after either process restarts, and removes it after the last
active-or-paused Service disappears. Operators do not manually run this
profile during ordinary use.

Caddy receives no Docker socket. Its root filesystem is read-only, all Linux
capabilities are dropped, resources are bounded, and its admin API exists only
at `/run/autonomy-service-gateway/admin.sock` in a named volume shared with the
dashboard. Session containers do not receive that volume. Certificate and key
files are read-only under `/run/autonomy-service-gateway-certs`, sourced from
the dashboard's verified `/run/autonomy-keycache/service-gateway` ramfs child;
Compose refuses a missing source instead of fabricating a disk-backed one.

The production file never maps Caddy to the host. The authenticated
`GET /api/network/service-gateway` projection reports observed lifecycle state
and successfully loaded route IDs without exposing certificate material or
upstream details. The narrowly scoped
`tools/network/acceptance/compose.service-gateway.yml` override maps loopback
8443 only for the real-node compatibility proof. Its harness and dependency-free
session canary are `python3 -m tools.network.acceptance.service_gateway` and
`tools/network/acceptance/service_gateway_canary.py`. Automatic lifecycle,
restart recovery, and timing are proved without a host port by
`python3 -m tools.network.acceptance.service_gateway_lifecycle`, which reaches
the internal gateway from the Dashboard container.

### Choosing where the data lives on the host

By default the three persistent volumes (`autonomy-code`, `autonomy-data`,
`autonomy-orgs`; `dolt-data` too, under the `beads` profile) live in Docker's
own internal volume storage (`/var/lib/docker/volumes/...`) — the operator
never sees a path. To put them on a specific host directory instead — a
separate disk, a location you back up directly, `/opt/autonomy` — layer
`deploy/docker-compose.host-data.yml` on top of the base file and point
`AUTONOMY_HOST_DATA_ROOT` at the parent directory:

```bash
mkdir -p /opt/autonomy/{code,data,orgs,dolt}   # root-owned parent (e.g. /opt) needs a one-time sudo mkdir+chown first
AUTONOMY_HOST_DATA_ROOT=/opt/autonomy AUTONOMY_FIRST_ORG=myorg docker compose \
  -f docker-compose.yml -f deploy/docker-compose.host-data.yml up -d
```

This still creates real, named Docker volumes (`docker volume inspect
autonomy-code` works, `docker compose down` without `-v` still leaves them
alone) — only their storage backing changes, via the `local` driver's bind
option (`type: none, o: bind, device: <path>`). That distinction is not
cosmetic: Docker auto-seeds an *empty named volume* from the image's baked-in
`/app` content the first time it's mounted; a plain bind mount
(`- /host/path:/app` written directly into the service's own `volumes:`)
never gets this treatment — it just shadows the image's `/app` outright,
hiding even the entrypoint, and the container fails to start. Verified live
2026-08-25 both ways (see `deploy/docker-compose.host-data.yml`'s own
header for the failure mode) — use the override file's `driver_opts` form
to relocate these volumes, never a raw bind.

The container entrypoint (`deploy/entrypoint.sh`) runs the idempotent
migrate-on-mount initializer and then uvicorn, serving HTTPS when the
init-generated keypair is present (`DASHBOARD_TLS=off` for plain HTTP behind
your own proxy). `AUTONOMY_FIRST_ORG` founds a new organization. Supplying
`AUTONOMY_INVITE` instead joins the existing organization named by that
user-carried invitation. `AUTONOMY_FLEET_INVITE` instead joins the operator's
existing personal Fleet: first run creates no shared organization, displays
the comparison code while the request waits, and stores the machine identity
only after browser-local completion of the approved delivery. All three
first-run modes are mutually exclusive. For an
`org:join` link, `graph link publish` prints the version-2 invitation code.
It keeps the registry's URL-path channel token distinct from the
fragment-carried ledger claim token; version-1 one-token codes are refused.

A production installation does not accept a mounted personal-passphrase
file. With no one-time stdin password, a fresh headless node validates and
stages the invitation without minting an identity or membership claim; the
personal identity ceremony remains an explicit interactive operation.

The workflow simulation has a separately named, doubly guarded mounted-file
input for synthetic test identities. It is documented only in
`deploy/harness/README.md` and is not mounted by the production dashboard.

A bearer join that needs approval persists only public resume coordinates in
`pending_joins.db`; restarting with the same invitation resumes and finalizes
at the server-recorded claim position after approval. The bearer, password,
root seed, and armor are never written to that store. An already-identified
running node does not accept the invitation through a server-side password
handoff. The production dashboard exposes no mounted-passphrase join endpoint;
interactive acceptance remains a browser-local identity ceremony.

### Verified published images

Building from your checkout remains the default Compose path above. A release
operator may additionally publish the node and session image family to any
OCI registry they choose. The registry is distribution only: trust comes from
the project-controlled cosign key and the immutable digest.

Production signing-key provisioning is intentionally outside the repository
and outside CI:

1. The project signing key is a cosign key pair. Its private key is sealed in
   the **secured** tier of the Autonomy organization's vault as
   `cosign-release-key` (provisioned 2026-09-26). A secured secret is released
   only when the operator approves the request, so that approval is the human
   act that authorizes every signing. The key carries no passphrase of its own.
2. The private key is never committed, copied to a runner, or stored as a CI
   secret; a released copy lives only in the requesting session's private
   ramfs for the duration of one signing run.
3. Only the public key is committed, as `deploy/cosign.pub`. Downstream
   verification fails closed against it; no placeholder key is accepted.
4. Registry credentials are stored as `AUTONOMY_REGISTRY_USERNAME` and
   `AUTONOMY_REGISTRY_PASSWORD`.

The release tooling uses cosign v3. Signing passes a signing config that names
no services, so nothing contacts Rekor or Fulcio.

The manual **Build and publish Autonomy images** workflow takes a registry host,
namespace, and release label. Its self-hosted `autonomy-release` runner uses
the existing source builds (`deploy/Dockerfile` and `agents/build.sh`) to
publish the node plus base/dashboard/DinD/host-terminal session images. Every pushed tag is
resolved to `image@sha256:...`. The workflow has no signing key and cannot
authorize a release; it emits an **unsigned** `image-lock.env` artifact:

```dotenv
AUTONOMY_IMAGE_LOCK_VERSION=1
AUTONOMY_RELEASE_TAG=v1.2.3
AUTONOMY_NODE_IMAGE=registry.example/autonomy/autonomy-node@sha256:...
AUTONOMY_SESSION_IMAGE=registry.example/autonomy/autonomy-session@sha256:...
AUTONOMY_SESSION_PLATFORM_IMAGE=registry.example/autonomy/autonomy-session-platform@sha256:...
AUTONOMY_SESSION_DIND_IMAGE=registry.example/autonomy/autonomy-session-dind@sha256:...
AUTONOMY_HOST_TERMINAL_IMAGE=registry.example/autonomy/autonomy-host-terminal@sha256:...
AUTONOMY_SERVICE_GATEWAY_IMAGE=registry.example/autonomy/autonomy-service-gateway@sha256:...
```

Set `AUTONOMY_IMAGE_SOURCE=https://github.com/<org>/<repo>` when publishing:
every image is then stamped `org.opencontainers.image.source`, which is what
GHCR reads to attach a package to its repository, and a first-time package
inherits the repository's visibility instead of being created private (an
installer's anonymous pull is refused until an operator changes it by hand).

After inspecting that lock, the operator performs the release-signing act on
their controlled machine:

```bash
AUTONOMY_COSIGN_VAULT_KEY=cosign-release-key \
  ./deploy/sign-image-lock.sh image-lock.env
```

The command displays every exact digest, then requests release of the secured
vault key; the operator's approval authorizes the signing, and the released
key is deleted when the run ends. A local password-armored key file remains
supported through `AUTONOMY_COSIGN_PRIVATE_KEY`, with a typed release-tag
confirmation before cosign prompts for its passphrase. Either way the script
refuses environment-backed keys and a preset `COSIGN_PASSWORD`, signs only
`image@sha256` references, uploads nothing to a transparency log, and verifies
every published signature immediately against `deploy/cosign.pub`. Thus Fulcio, Rekor, GitHub
OIDC, and a hot CI signing secret are not part of the trust path.

Before installing or swapping an image, verify the exact lock entry:

```bash
./deploy/verify-image.sh \
  registry.example/autonomy/autonomy-node@sha256:<64-lowercase-hex>
# Equivalent direct contract:
cosign verify --insecure-ignore-tlog --key deploy/cosign.pub \
  registry.example/autonomy/autonomy-node@sha256:<64-lowercase-hex>
```

The helper rejects tags such as `:latest` or `:v1.2.3`; verification is never
authorization to follow a mutable tag. After verification, use the same
digest in Compose. The explicit `--insecure-ignore-tlog` means “the tracked
project key is the trust root; do not require Rekor,” not “skip signature or
digest validation.”

To start a node from a signed release without a checkout or any build, use
`deploy/install-published.sh`. It is a single self-contained file that embeds
the project public key. It verifies every digest in the lock before pulling
anything, takes the Compose file out of the verified node image, tags the
session images under the names the launcher uses, and waits for `/api/ping`:

```bash
bash install-published.sh --lock <image-lock.env URL or path> --install-docker --yes
```

The signed digest is also what the product workflow simulation builds from;
see `deploy/harness/README.md`.

#### Upgrading a node to a new release

Rerun the installer with the newer release's lock and the same `--dir`:

```bash
bash install-published.sh --lock <newer image-lock.env> --dir ~/autonomy --yes
```

The node keeps its data, identity and settings. What the rerun does:

1. Verifies and pulls the release exactly as a first install does.
2. Checks and moves the code. The node runs its code from the `autonomy-code` volume, not
   from the image: Docker seeds that volume from the image only while it is
   empty, so a new image alone would run the old code. Before any container
   is recreated, a one-shot container of the new node image (as uid 1000,
   the volume's owner) fetches the commit named in the image's `/app/VERSION`
   into the volume's repository and moves it there with `git reset --keep`.
   The installer checks first, and refuses with one line, changing nothing
   (not the containers, not the session image tags, not `--dir`), when the
   volume has uncommitted changes, or when its HEAD is not an ancestor of the
   release commit: a downgrade, or local commits a developer node carries.
   `--allow-downgrade` overrides the ancestry check only; uncommitted changes
   are always refused. Only then does it stop every container of the Compose
   project `autonomy` and move the volume, because the dashboard hot-reloads
   from it and would otherwise run the new code on the old image.
3. Replaces `docker-compose.yml` with the one in the new node image and
   leaves `docker-compose.override.yml` alone.
4. Merges `.env`: `AUTONOMY_IMAGE` and `AUTONOMY_SERVICE_GATEWAY_IMAGE` are
   set from the lock; every other value is kept. `DASHBOARD_PORT` and
   `AUTONOMY_HOST_HOME` change only when `--port` or `--host-home` is passed
   again, `DASHBOARD_HTTP_PORT` only with `--http-port`, and `AUTONOMY_SUBNET`
   is chosen only when the key is missing.
5. Runs `docker compose up -d --no-build` and waits for `/api/ping`.
6. Records the release in the data volume once the dashboard answers:
   `release/installed.env` is the installed lock plus `AUTONOMY_INSTALLED_AT`,
   and the record it replaces moves to `release/history/`. A first install
   writes the same record.

Afterwards the code volume's HEAD is the commit in the release image:

```bash
docker run --rm --entrypoint cat <node image@sha256:...> /app/VERSION
docker compose exec -u autonomy dashboard git -C /app rev-parse HEAD
docker compose exec -u autonomy dashboard cat /app/data/release/installed.env
```

A node that follows `origin/master` through the dashboard's software update
will usually be ahead of the newest release, so the ancestry check refuses it
as a downgrade. Stay on the update channel the node already uses, or pass
`--allow-downgrade` once on purpose.

##### Converting a node built from a checkout

A node started with `docker compose up` from a checkout (SJC-2's
`~/autonomy-shipped`, for example) has its Compose project directory in that
checkout. Its containers and volumes still belong to the Compose project
`autonomy` (`name: autonomy` in `docker-compose.yml`) and its volumes have
pinned names (`autonomy-code`, `autonomy-data`, ...), so an installer
directory takes over the same containers and volumes. The data is not copied
and nothing is rebuilt.

1. Create the new directory and carry over the node's own values from the old
   `.env`. Copy at least `DASHBOARD_PORT`, `DASHBOARD_HTTP_PORT`,
   `AUTONOMY_SUBNET` and `AUTONOMY_HOST_HOME` (otherwise the installer chooses
   new ones: a new subnet recreates the project network), plus anything else
   the node sets: `TZ`, data-root and bind-path settings, credentials. Do not
   carry `AUTONOMY_IMAGE`; the installer sets it.

   ```bash
   mkdir -p ~/autonomy
   grep -v '^AUTONOMY_IMAGE=' ~/autonomy-shipped/.env >~/autonomy/.env
   ```

2. Copy `docker-compose.override.yml` if the checkout has one. The installer
   runs a plain `docker compose up`, so a node started with extra `-f` files,
   such as `deploy/docker-compose.host-data.yml` for host-directory volumes,
   needs those files copied into the new directory and named in `.env`, for
   example `COMPOSE_FILE=docker-compose.yml:docker-compose.host-data.yml`
   (with `COMPOSE_FILE` set, Compose no longer loads
   `docker-compose.override.yml` by itself: list it there too), together
   with the variables they read, such as `AUTONOMY_HOST_DATA_ROOT`.
   Check the result with `docker compose config` in the new directory
   before running the installer.
3. Commit or discard any uncommitted change in the code volume
   (`docker compose exec -u autonomy dashboard git -C /app status`). A node
   whose code predates the 2026-10-01 history rewrite
   (`deploy/PUBLIC-REPO-CLEANSE.md`) has a HEAD from the old history, which
   is never an ancestor of a release commit: reset it to the rewritten
   history first, as that runbook says, or pass `--allow-downgrade` for this
   one run after checking that the volume holds nothing you need.
4. Run the installer with `--dir ~/autonomy`. From then on, upgrade from that
   directory only. Running `docker compose` from the old checkout would
   recreate the same containers from its own compose file and image
   reference; retire it (rename the directory, or delete it once the new
   one has run).

This presentation path refuses source builds and verifies the exact digest
before starting any container. It opens the HTTPS dashboards, real relay note,
graph, and Design Studio surfaces between narratable pauses, and produces a
timestamped transcript and URL manifest. The HTTPS certificates are local
self-signed node certificates pinned by the driver; they are not represented
as public-CA endorsements. The operator records the live run separately—the
command never manufactures a screen-capture artifact.

```bash
AUTONOMY_IMAGE='registry.example/autonomy/autonomy-node@sha256:<digest>' \
  docker compose pull dashboard
AUTONOMY_IMAGE='registry.example/autonomy/autonomy-node@sha256:<digest>' \
  docker compose up -d --no-build dashboard
```

This is an additional verified-published path. It does not replace the
no-login checkout build and does not add a runtime CDN or phone-home.

#### A free-threaded (Python 3.14t) release

By default the node image is built `FROM python:3.12-slim` with `--pull`.
Setting `AUTONOMY_PYTHON=3.14t` builds it on free-threaded CPython 3.14
instead. Docker Hub publishes no free-threaded Python image, so the script
first builds the base `autonomy-python:3.14t-slim` from
`deploy/Dockerfile.python-freethreaded` (with `--pull`, so its Debian base is
fresh), then builds the node with `BASE_IMAGE=autonomy-python:3.14t-slim` and
**without** `--pull` — a pull of that local-only base would fail with
"pull access denied for autonomy-python". Naming that base in
`AUTONOMY_BASE_IMAGE` selects the same mode; any other base image with
`AUTONOMY_PYTHON=3.14t` is refused. The session images and the Service
gateway are unchanged. The base is only a build input: it is neither pushed
nor listed in the lock.

```bash
AUTONOMY_PYTHON=3.14t \
AUTONOMY_REGISTRY=ghcr.io AUTONOMY_IMAGE_NAMESPACE=auto-network \
AUTONOMY_RELEASE_TAG=<tag> AUTONOMY_IMAGE_SOURCE=https://github.com/<org>/<repo> \
  ./deploy/publish-images.sh
```

The lock then carries `AUTONOMY_PYTHON=3.14t` after the release tag (a default
release has no such line). It is not an image entry: signing and verification
cover the same digests, and `install-published.sh` reports the flavour in its
`release` step. Before publishing the signed lock, confirm the node really runs
without the GIL:

```bash
docker run --rm --entrypoint python3 <AUTONOMY_NODE_IMAGE from the lock> \
  -c "import sys; print(sys._is_gil_enabled())"   # must print False
```

#### Base-image CVE republishing

Release engineering reviews the node and session base images at least weekly
and on every upstream critical/high CVE notice. A patch release is a complete
rebuild with `--pull`, never an in-place package update:

1. update deliberately pinned base/tool versions when required;
2. run the build/publish workflow with a new release label;
3. retain the prior `image-lock.env` for rollback;
4. inspect the new digest lock and invoke `deploy/sign-image-lock.sh` with the
   operator-held armored key;
5. publish the signed lock artifact and its release notes, including which base
   CVEs motivated the rebuild;
6. verify every new digest before recommending a swap.

Old tags may remain for rollback, but a digest is never overwritten and an
old signature is never treated as authorization for a rebuilt artifact.
Session containers are not updated in place; newly launched sessions use the
new verified session-image digests.

#### Retention: keep every release; withdraw one deliberately

Every signed release stays in the registry. Public packages cost nothing to
store, an old image is the only exact evidence of what a release contained,
and rollback can go back any number of releases. A rebuild from git is not a
substitute: the build pulls fresh base images, so neither the bytes nor the
digest match. A tag is never moved to new content: releases are pinned and
signed by digest, so a moved tag would match neither its lock nor its
signature.

A release is deleted only to withdraw it: it leaked content that must not be
public (as on 2026-10-02, see `deploy/PUBLIC-REPO-CLEANSE.md`), or it is
broken or vulnerable and must not be installed. Withdrawing a release means
deleting its package versions and deleting its lock file from
`deploy/releases/` in the same change, so that directory lists only releases
that can be installed; git history keeps the withdrawn lock as the record.

The packages belong to the `auto-network` GitHub account, and deleting needs
a token with `delete:packages`, which the publishing token does not carry.
Sign in for this one run with a separate `gh` configuration, so the
operator's own login is untouched:

```bash
export GH_CONFIG_DIR="$(mktemp -d)"
gh auth login -h github.com -s read:packages,delete:packages --web
# the operator enters the printed code at github.com/login/device,
# signed in as auto-network
```

Build the keep set before deleting anything. It is every image digest named
in a lock that stays under `deploy/releases/`, plus every manifest reachable
from those digests and from their `sha256-<digest>` signature tags. cosign v3
stores a signature as a tagged index whose child, the Sigstore bundle, is an
**untagged** version: a rule that keeps tagged versions and deletes untagged
ones deletes the signatures of the releases being kept. Delete each version
outside the keep set with
`gh api -X DELETE /users/auto-network/packages/container/<package>/versions/<id>`,
then run `deploy/verify-image.sh` on every digest in every remaining lock,
list each package's tags to confirm only the kept releases remain, and sign
out (`gh auth logout`) and remove the temporary configuration directory.

### Volume layout & backup

One named volume, `autonomy-data`, mounted at `/app/data`, holds **all**
persistent state:

| Path in volume | Roots via | What it is |
|---|---|---|
| `orgs`/ | `AUTONOMY_ORGS_DIR` | per-org graph DBs — identity Settings and credential rows (**the secret store**; organizations only) |
| `personal.db` | beside `orgs/` (roots with `AUTONOMY_ORGS_DIR`) | the operator's own store — follows them across their fleet; not an organization |
| `machine.db` | beside `orgs/` (roots with `AUTONOMY_ORGS_DIR`) | this machine's own store — never leaves this computer; not an organization |

### Relocating the local stores (one-time, manual)

Installations that predate the local-store split still have `personal.db`
(and possibly `machine.db`) inside `data/orgs/`. **Everything keeps working
from that location indefinitely** — resolution serves whichever location
holds the store — so do this whenever convenient, not urgently:

```bash
# stop the dashboard first; nothing may hold the store open
sqlite3 data/orgs/personal.db "PRAGMA journal_mode=DELETE;"
mv data/orgs/personal.db data/personal.db
sqlite3 data/orgs/machine.db  "PRAGMA journal_mode=DELETE;"   # if present
mv data/orgs/machine.db  data/machine.db                      # if present
ls data/orgs/*.db-wal 2>/dev/null   # MUST print nothing before you start
# start the dashboard; it returns each store to WAL on first open
```

**The PRAGMA line is not optional, and skipping it silently loses data.**
These stores run in SQLite WAL mode: committed rows can live in a
`personal.db-wal` sidecar while the `.db` file itself is little more than a
header. Moving the `.db` alone orphans that sidecar — the new location
reads as an EMPTY store, nothing raises, and every read quietly falls back
to the organization's value as if the operator had never written one.
`PRAGMA journal_mode=DELETE` folds the WAL into the main file and removes
the sidecars, making the store one self-contained file that a single `mv`
moves whole. The `ls` check is the one-line proof the fold happened: any
surviving `*.db-wal` beside the old location means committed data is about
to be left behind — stop and fold before moving.

Do it with the dashboard stopped so nothing holds the files open (the
PRAGMA refuses on a busy store, which is itself a check). There is
deliberately no automated migration: the store is the identity armor, and
a quiet manual move beats any amount of crash-safety machinery around a
live one.
| `graph.db` | `GRAPH_DB` | main knowledge-graph DB |
| `dashboard.db` | `DASHBOARD_DB` | dashboard operational store |
| `auth.db` | `AUTH_DB` | dashboard auth store |
| `dispatch.db` | `DISPATCH_DB` | dispatch operational store |
| `approval_requests.db` | `APPROVAL_REQUESTS_DB` | approval-request store |
| `web-push.db` | `WEB_PUSH_DB` | Web Push subscriptions and bounded transport outbox (never inbox truth) |
| `commit_workflow.db` | `COMMIT_WORKFLOW_DB` | commit-workflow store |
| `mission_control.db` | `MISSION_CONTROL_DB` | Mission Control store (missions + site revisions) |
| `mcp_relay.db` | `MCP_RELAY_DB` | MCP-relay peer store (per-openai-session org bindings + crosstalk grants) |
| `dashboard_identity_sessions.db` | `DASHBOARD_IDENTITY_SESSION_DB` | identity unlock-session store |
| `dashboard-session.secret` | roots with `DASHBOARD_IDENTITY_SESSION_DB` | mode-0600 Dashboard session-token and local access-result destination key; moves with the identity-session realm |
| `pending_joins.db` | `AUTONOMY_PENDING_JOINS_DB` | restart-safe invite-join progress (identifiers and counts only) |
| `network`/ | `AUTONOMY_NETWORK_KEY_DIR` | mode-0600 auto.network tunnel-serving delegate keys |
| `web-push-keys`/ | `WEB_PUSH_KEY_DIR` | mode-0700 VAPID keyring for Dashboard Web Push (mode-0600 key files) |
| `tls.crt` | `AUTONOMY_TLS_CERT` | TLS certificate (self-signed by default) |
| `tls.key` | `AUTONOMY_TLS_KEY` | TLS private key |
| `.beads`/ | `BEADS_DIR` | beads tracker config — per-org Dolt SQL credentials (mode-0600 `credentials.env`), `config.yaml`, `metadata.json`; backed up as config files only, never Dolt data |
| `agent-runs`/ | `DASHBOARD_AGENT_RUNS_DIR` | session artifacts |
| `session-traces`/ | `DASHBOARD_TRACE_DIR` | session traces |
| `dropbox`/ | `AUTONOMY_DROPBOX_DIR` | machine-global operator dropbox objects and receipt metadata |
| `browser-profiles`/ | `BROWSER_PROFILES_DIR` | persistent browser profiles by organization and workspace (`<org>/<workspace>/<name>`) — site sign-in state; not backed up |

**Secret files kept on disk on purpose** (`pre_sign_in` in the manifest;
operator ruling 2026-10-01, auto-es7ja): only those needed *before* the
operator signs in, since sign-in is what warms the vault after a reboot —
`tls.key` (serves the unlock page), `dashboard-session.secret` (signs the
sign-in cookie), the VAPID keyring `web-push-keys/` (pushes are signed from
dashboard start), and the beads
`credentials.env` files (the dispatcher and the backup reach Dolt while the
vault is cold). Every other node-generated secret is a vault row or a ramfs
file re-minted at start.

Every row is **test-coupled** to `tools/data_paths.py::STORE_MANIFEST`,
which is also what the resolvers read: `test_volume_contract.py` fails if a
manifest store is missing from this table, so a store can never be added to
the contract and silently omitted here. (The coupling catches omissions, not
a stale description or a hand-added row with no manifest entry — those need
a real generator, which this table does not yet have.) Each store resolves **environment variable first**,
then the volume root, then the repository-local default; setting a
variable therefore moves that store for *every* reader at once. With
`AUTONOMY_REFUSE_REAL_DATA_FALLBACK=1` an unrooted store raises instead of
silently falling back to the operator's live `data/` tree.

Verified by `tools/tests/test_volume_contract.py`, which roots a volume at
a temporary directory, runs first-run init, and asserts the operator's real
`data/` and `$HOME` are byte-unchanged — and that deliberately un-rooting a
store is caught.

Backing up the deployment = backing up that volume. The image is disposable;
the volume is not.

#### Portable snapshot and restore

`python -m tools.portability` turns the complete manifest-rooted node volume
into one validated artifact. Version 1 is intentionally **quiesced-only**:
SQLite can make each database internally consistent while it is live, but no
cross-store writer barrier currently makes several databases plus TLS/key
files share one coherent hot point. Stop the dashboard before acknowledging
`--quiesced`; the tool refuses to create an artifact without that explicit
acknowledgement.

With the dashboard stopped, run the tool in a one-shot container that mounts
the same volume:

```bash
docker compose stop dashboard
docker compose run --rm --no-deps --entrypoint python3 \
  -v "$PWD:/backup" dashboard \
  -m tools.portability snapshot /app/data /backup/node-snapshot.tar.gz \
  --quiesced
```

Every SQLite store (including every `orgs/*.db`) is copied through SQLite's
backup API; WAL sidecars are never copied. Every resulting file is recorded
with its size, mode, and SHA-256 digest in the artifact manifest. Restore
accepts only regular, traversal-free members, verifies the manifest identity,
all file hashes, the exact `STORE_MANIFEST` shape, and SQLite integrity, then
materializes into an **absent fresh** volume path:

```bash
python -m tools.portability restore \
  node-snapshot.tar.gz /path/to/fresh-node-data
```

A torn, altered, structurally incomplete, or newer-format artifact is refused;
restore never overlays an existing volume.

The optional Dolt/beads backend lives in the separate `dolt-data` volume and
is therefore not silently claimed as part of `/app/data`. When beads is in
use, export a consistent `beads.sql` while Dolt is also quiesced and pass
`--beads-present --beads-dump beads.sql` to snapshot. Declaring beads without
the dump fails closed. Restoring such an artifact requires
`--beads-output /fresh/path/beads.sql`, ready for import into a fresh Dolt
volume; omitting it is also refused.

Each mounted volume carries `.autonomy-volume.json`. Container startup runs
`migrate-on-mount` before the dashboard: legacy/unversioned volumes migrate
forward through the existing idempotent store initializers, while a volume
written by a newer node is rejected before any mutation. Restoring preserves
the personal/org root material, ledgers, memberships, configuration, and TLS
keys byte-for-byte. On a new machine, use the existing personal password or
recovery path when a machine-bound passkey factor is unavailable; plaintext
roots still never leave the client. Relay re-announcement is a serving-layer
startup concern and is exercised by the workflow simulation.

The older `tools/graph/backup-*.sh` jobs remain useful rolling/offsite
single-store backups. They are not a claim of cross-store coherent
portability; use the quiesced artifact above for a node move.

### Optional beads (issue tracker) backend

`docker compose --profile beads up -d` adds a `dolt` SQL-server service
(image operator-chosen via `DOLT_IMAGE`). Without it — the default — the
dashboard runs fine: beads surfaces degrade to empty with a single logged
warning. The beads DAO honors `DOLT_SQL_HOST` / `DOLT_SQL_PORT` /
`DOLT_SQL_USER` / `DOLT_SQL_PASSWORD` / `DOLT_SQL_DATABASE` for any
topology. Writing beads additionally needs the `bd` CLI, installed
separately.

Autonomy roots itself **relatively**: every core path derives from the repo
checkout (`Path(__file__)` walked up to the repo root) and the running user's
home (`Path.home()`). A clean `git clone` at *any* path boots without editing a
single file — there are no hardcoded operator host paths in `tools/` or
`agents/` (pinned by `tools/graph/tests/test_no_hardcoded_host_paths.py`).

The environment variables below are **overrides**, not requirements. Each has a
sane default derived from the checkout, so the out-of-box behaviour on a fresh
clone is correct. Set them only when your deployment layout differs from the
default (e.g. a container whose mount points don't match the host).

## Portability / path roots

| Variable | Default | Purpose |
|---|---|---|
| `AUTONOMY_ROOT` | script's repo root (`<script>/../..`) | Repo root used by the shell tooling (`tools/graph/backup-*.sh`, `tools/dashboard/renew-tls-cert.sh`). |
| `AUTONOMY_HOST_ROOT` | repo root (`Path(__file__)`-derived) | Canonical host repo prefix that container-observed session paths are rewritten to (session-trace dedup identity). |
| `AUTONOMY_HOST_HOME` | `Path.home()` | Canonical host home prefix for the same rewrite. Required on a Compose node: the operator's home is mounted read-only at `/host-home` in the dashboard and the host terminal, for credential bootstrap (`graph credentials import --home /host-home`) and transcript ingest; nothing there is written. `deploy/quickstart.sh` records it in `.env`. |
| `AUTONOMY_CONTAINER_ROOT` | `/workspace/repo` | Container mount point of the repo, the *source* prefix rewritten to `AUTONOMY_HOST_ROOT`. |
| `AUTONOMY_CONTAINER_HOME` | `/home/agent` | Container home mount, the *source* prefix rewritten to `AUTONOMY_HOST_HOME`. |
| `AUTONOMY_HOST_PROJECT_ORGS` | *(unset)* | JSON object `{"<abs-cwd>": "<org-slug>"}` overlaying the Claude-Code project-dir → org routing table (`tools/graph/ingest.py`). Keys are absolute cwds; they are slugified internally. |

On the primary host these defaults already resolve to the operator's real
layout (e.g. `AUTONOMY_HOST_ROOT` → the checkout under the operator's home), so
no configuration is needed there either.

## Data & databases

| Variable | Default | Purpose |
|---|---|---|
| `AUTONOMY_ORGS_DIR` | `<repo>/data/orgs` | Per-org graph DB directory (`<slug>.db`). |
| `AUTONOMY_FIRST_ORG` | unset | Slug of a first shared org to create at first-run init / dashboard startup, for deploys that name one. Unset, no organization is created: the operator creates or joins one in onboarding, or has none. |
| `AUTONOMY_FIRST_ORG_NAME` | title-cased slug | Display name seeded into the first org's `autonomy.org#1` identity Setting. |
| `GRAPH_DB` / `GRAPH_API` | `<repo>/data/graph.db` / *(unset → local DB)* | Graph DB path, or a remote graph API base URL. |
| `DASHBOARD_DB` | `<repo>/data/dashboard.db` | Dashboard overlay DB. |
| `DASHBOARD_IDENTITY_SESSION_DB` | `<repo>/data/dashboard_identity_sessions.db` | Local revocation and history store for human dashboard sessions. Its `dashboard-session.secret` is always resolved beside this database so cookies and local approval results cannot split into different realms. Keep both writable; verification fails closed if either is unavailable. |
| `DISPATCH_DB` | `<repo>/data/dispatch.db` | Dispatch state DB. |
| `APPROVAL_REQUESTS_DB` / `COMMIT_WORKFLOW_DB` | `<repo>/data/*.db` | Approval-request and commit-workflow DBs. |
| `MISSION_CONTROL_DB` | `<repo>/data/mission_control.db` | Mission Control (missions + site revisions) DB. |
| `DASHBOARD_AGENT_RUNS_DIR` | `<repo>/data/agent-runs` | Where agent-run session traces land (the container→host handoff dir). |
| `DASHBOARD_TRACE_DIR` | `<repo>/data/session-traces` | Session trace output. |

## Service / runtime

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_DOMAIN` | the existing certificate's `.ts.net` name | Tailscale hostname the TLS-cert renewal issues for (`renew-tls-cert.sh`); when unset the script renews the name found in the current certificate's subjectAltName and logs a refusal to `data/cert-renew.log` when neither exists. Also seeds the dashboard's recorded remote-access origin once, on nodes onboarded before that step existed. |
| `DASHBOARD_URL` | `https://localhost:8080` | Dashboard base URL for CLI/tools. |
| `DASHBOARD_AUTH` | *(unset; enforced)* | Recovery kill-switch for the human unlock gate. Only `off`, `0`, `false`, or `no` (case-insensitive) disable enforcement — use to recover from a broken unlock, then unset. Unset or any other value enforces (fail-safe). |
| `DOLT_BIN` | `dolt` on `PATH` | Path to the `dolt` binary used by `backup-all.sh`. |
| `GRAPH_SCOPE` / `GRAPH_ORG` | *(unset)* | Scope graph CLI access to an org (see `graph-<project>` wrappers). |
| `AUTONOMY_SESSION` | *(set by launcher)* | Current session's tmux/routing name. |

The revocable-session migration intentionally does not grandfather old
stateless dashboard cookies. After deploying it, each open browser unlocks the
dashboard once to create its server-side session record. This is expected, not
a lost identity or passkey. `DASHBOARD_AUTH=off` remains the recovery path if
the session database is unavailable during rollout; remove the override after
the store is writable.

## Product workflow acceptance

On a Linux Docker host, the acceptance test is a browser driving the real user
interface against isolated services:

```bash
node deploy/harness/workflow.mjs
node deploy/harness/fleet-workflow.mjs
```

The first covers organization invitation, join, approval and synchronization.
The second covers personal fleet enrollment, and ends by serving a published
note from the second machine after the publishing dashboard is stopped. See
[`deploy/harness/README.md`](deploy/harness/README.md) for the scenarios,
options and the rule that no harness step may perform a product transition.

Volume snapshot and restore onto a fresh machine is covered without Docker by
`tools/tests/test_portability.py`.

## Clean-clone smoke

To verify a checkout is portable, clone to a path that is neither the operator
home nor the container mount and exercise the entry points:

```bash
git clone <repo> /tmp/autonomy-clean
cd /tmp/autonomy-clean
python -m tools.graph.cli --help
python -c "import tools.dashboard.server as s; print('dashboard import OK')"
pytest tools/graph/tests/test_no_hardcoded_host_paths.py -q
```

All three must succeed with no absolute operator paths baked in.
