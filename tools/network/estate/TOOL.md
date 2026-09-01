# estate — auto.network deterministic infrastructure

Cattle-not-pets provisioning for auto.network services on Hetzner Cloud
(direction: graph note `8cb2a39c-4bc`; work tracking: bead `auto-pqcsh`).
Every box must be reproducible from these scripts — nothing hand-configured.

## Primitives (the contract consumers code against)

| Script | Contract |
|---|---|
| `provision-vm.sh <name> [--role r] [--type cpx11] [--location ash]` | Clean Ubuntu 24.04 VM with caddy/rsync/python3-venv, estate-labeled, firewalled (22/80/443/icmp). Prints IPv4 on stdout. Idempotent on name |
| `teardown-vm.sh <name>` | Deletes the VM. Refuses non-estate-labeled servers and the legacy pet unconditionally. Volumes untouched |
| `list-vms.sh` | Project inventory: estate VMs + the pet, one line each |
| `namecheap_dns.py {gethosts,add-record}` | Safe Namecheap read-modify-write. `add-record` appends EXACTLY ONE record: reads the live set, refuses unless the critical mail records survived, `%2B`-encodes DKIM, writes the full set, and re-reads to prove +1/−0. Runs on auto-ash-1 (whitelisted IP). Unit-tested in `tests/` |
| `add-apex-a-record.sh [--dry-run]` | Pinned wrapper: adds the one authorized apex A `@ → 5.161.219.195` via `namecheap_dns.py`. Run on auto-ash-1 |
| `deploy-caddy.sh [ssh-target]` | Installs the complete repository-owned four-block Caddyfile on registry-ash-1: checksum, target-side `caddy validate`, atomic replace, rollback copy, reload. Never captures the live host as source. Default target `root@5.161.219.195` |
| `verify-apex.sh` | End-to-end proof: apex DNS across 4 public resolvers, valid cert, `/healthz` 200, `/install` content contract, and that share links still issue on relay.auto.network |
| `namecheap_dns.py add-delegation` | Appends EXACTLY the four serve.auto.network NS+glue records (auto-g1jxw): same read→gate→write→verify discipline, +4/−0 required, NS-aware append, `--dry-run` renders the operator-approval diff. Runs on auto-ash-1 |
| `dns/` | The registry-served serve.auto.network authoritative zone (auto-g1jxw): its own systemd process on the relay host answering from registry state, 53/tcp+udp firewall, verify script, gated 4-record parent delegation, full runbook in `dns/README.md` |

Consumers: `tools/network/registry/deploy/` (share-links registry), the
H2 clean-VM deployment harness, H6 clean-machine acceptance.

## Public front door: auto.network apex (bead auto-9q7a5)

The canonical public front door — the agent-first `/install` URL and
`/healthz` — is served at `https://auto.network` from **registry-ash-1**
(`5.161.219.195`), the SAME box and process (`127.0.0.1:8477`) that already
answers `registry.auto.network` and `relay.auto.network`. The hostname split
is a naming/contract boundary, decided and fixed:

| Host | Serves |
|---|---|
| `auto.network` | public front door + agent-first `/install` + `/healthz` |
| `registry.auto.network` | registry / control API compatibility |
| `relay.auto.network` | issued share links + viewer WebSockets |

Adding the front door does **not** change the registry `--base-url`: newly
minted share links keep issuing on `relay.auto.network`. See `caddy/README.md`.

### Runbook — order matters (DNS before cert)

Caddy mints the auto.network certificate over HTTP-01, which needs the apex to
already resolve to the box. So DNS lands first, then the vhost:

```bash
# 1. On auto-ash-1 (5.161.179.179) — its IP is Namecheap's whitelisted client.
ssh -o IdentitiesOnly=yes -i ~/.ssh/auto root@5.161.179.179
cd <repo>/tools/network/estate
./add-apex-a-record.sh --dry-run     # read + gate + encoding-check, no write
./add-apex-a-record.sh               # read → gate → write → verify +1/−0
#    pre-change set saved verbatim to /var/backups/namecheap/auto.network.before.xml

# 2. Anywhere with public DNS — wait until the apex has propagated.
dig +short @1.1.1.1 auto.network A   # expect 5.161.219.195

# 3. Install the complete Caddyfile on registry-ash-1.
./deploy-caddy.sh                     # checksum → validate → atomic install → reload

# 4. Prove it end to end (multi-resolver — never trust one).
./verify-apex.sh
```

**Rollback.** DNS: re-apply `auto.network.before.xml` (the saved full set) via
setHosts. Caddy: `cp /etc/caddy/Caddyfile.bak.<ts> /etc/caddy/Caddyfile &&
systemctl reload caddy` on registry-ash-1 (the backup path is printed by the
deploy).

## The Hetzner project — shared with a pet

These scripts operate in the project that also contains the **live mail
server** `ubuntu-ash-1` (OS hostname `auto-ash-1`, id 58635516,
5.161.179.179 — see graph note `dca4002d-a8a`). Guard rails, in depth:

1. The pet has Hetzner delete+rebuild protection enabled.
2. `teardown-vm.sh` only deletes servers labeled `managed-by=auto-network-estate`.
3. The pet's name/id are hard-refused in both scripts.

Never weaken these to "clean up" the project. The pet is migrated off and
retired via its own plan, not via estate teardown.

## Token

Resolution order: `$HCLOUD_TOKEN` → `$AUTO_NETWORK_TOKEN_FILE` (path to a
file with the bare token) → the `auto-network` context in
`~/.config/hcloud/cli.toml` (installed on the workstation 2026-07-17).
The token is interim and will be rotated once secret custody lands
(graph `8cb2a39c-4bc` open question 2). Never commit it, never write it
into graph notes, never mount all of `~/.config/hcloud` into a container
(it carries every context's token).

## Roadmap (bead auto-pqcsh)

- Golden snapshot build (`create-snapshot.sh`): hardened image replacing
  the stock-image + cloud-init path; provision contract unchanged.
- Site assembly (`build-site.sh`): multi-VM rollout from snapshots.
- Namecheap DNS scripting (read-modify-write the FULL record set —
  `setHosts` replaces all records). **Landed** as `namecheap_dns.py` +
  `add-apex-a-record.sh` (bead auto-9q7a5); generalise to a full DNS
  cutover script reusing the same safety gates.
- Install `caddy/registry-ash-1.Caddyfile` from the golden-snapshot build.
  The complete config is now authored and reproducible; do not reintroduce a
  live-host capture as a deployment input.
- Validation + full-site teardown, mirroring the BlindHash script set
  (`graph://c3d2061c-aee`, personal org).
