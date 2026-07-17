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

Consumers: `tools/network/registry/deploy/` (share-links registry), the
H2 clean-VM deployment harness, H6 clean-machine acceptance.

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
  `setHosts` replaces all records).
- Validation + full-site teardown, mirroring the BlindHash script set
  (`graph://c3d2061c-aee`, personal org).
