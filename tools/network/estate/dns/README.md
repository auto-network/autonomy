# serve.auto.network — delegated authoritative DNS (auto-g1jxw)

The delegated zone every sovereign Service origin lives under
(`<app>.<persona>.serve.auto.network`), served by an estate-owned
PowerDNS Authoritative pair with atomic per-RRset mutations — replacing
the parent zone's all-or-nothing Namecheap `setHosts` for everything
under `serve.`. Design of record: `graph://c880c5e6-8bd` §5.3–§5.4;
checkpoint: `graph://bdacdfd0-ebc`.

| Box | Role | Name |
|---|---|---|
| `dns-ash-1` | primary (sole writer, loopback API, challenge broker) | `ns1.serve.auto.network` |
| `dns-hel-1` | secondary (native AXFR/NOTIFY, no API) | `ns2.serve.auto.network` |

Zone content: SOA (refresh 300 / retry 60 / expire 604800), NS pair,
in-zone glue, apex + `*` A → the existing relay ingress
(`5.161.219.195`), wildcard TTL 300 for fast rollback. **DNSSEC is
explicitly unsigned — no DS anywhere**; signing is a later change with
its own ceremony.

## Security boundary

- The PowerDNS API binds `127.0.0.1:8081` on the primary only; its key
  is root-owned 0600 `/etc/autonomy-dns/api-key`, generated at deploy,
  never checked in, never delivered to dashboards or sessions.
- `challenge_broker.py` is the ONLY automated mutation path: TXT-only,
  name-bounded to `_acme-challenge.<label>.serve.auto.network`, TTL
  clamped [60, 300], ≤8 values/name, every present expiry-ledgered and
  purged by `autonomy-dns-purge.timer` (5 min). Simultaneous
  apex+wildcard values at one name are merged, never replaced. Its
  argv/exit contract is the seam auto-bhs3c bridges registry ops onto.
- The parent zone stays under the existing gated Namecheap wrapper; the
  new `add-delegation` subcommand appends EXACTLY four records with the
  same read→gate→write→verify (+4/−0) discipline and mail-record gates.

## Runbook — order matters, each step gated on the previous proof

```bash
# 0. From a host session holding the estate token + SSH key.
cd tools/network/estate

# 1. Provision the pair (distinct locations) + open 53.
./provision-vm.sh dns-ash-1 --role dns --location ash    # note IPv4 → P
./provision-vm.sh dns-hel-1 --role dns --location hel1   # note IPv4 → S
./dns/ensure-dns-firewall.sh

# 2. Deploy pinned PowerDNS + zone + broker; proves both boxes answer.
./dns/deploy.sh --primary root@P --secondary root@S

# 3. Staging proofs — no parent change yet. Retain the output.
./dns/verify-dns.sh P S

# 4. Backup/restore + primary-loss drills (bead acceptance; retain output).
./dns/backup-zone.sh root@P /var/backups/autonomy-dns
#    primary-loss: ssh root@P systemctl stop pdns; dig @S probe.serve.auto.network
#    (secondary keeps answering); systemctl start pdns
#    restore drill: on a scratch VM, ./dns/restore-zone.sh root@SCRATCH <backup> S

# 5. OPERATOR APPROVAL GATE — render the exact parent diff, no write:
#    (on auto-ash-1, the Namecheap-whitelisted host)
python3 namecheap_dns.py add-delegation --primary-ip P --secondary-ip S --dry-run

# 6. Apply the approved four-record delegation (same host):
python3 namecheap_dns.py add-delegation --primary-ip P --secondary-ip S
#    pre-change set saved verbatim to /var/backups/namecheap/auto.network.before.xml

# 7. Post-cutover proof: authoritatives + ≥2 independent public resolvers,
#    and capture before/after parent sets proving mail records unchanged.
./dns/verify-dns.sh P S --public
python3 namecheap_dns.py gethosts --save /var/backups/namecheap/auto.network.after-delegation.xml
```

## Rollback

- **Parent:** re-apply the saved pre-change set
  (`auto.network.before.xml`) via `setHosts` — removes exactly the four
  delegation records; resolvers forget the delegation within NS TTL
  (3600 s worst case). The DNS boxes can stay up for a retry.
- **Zone content:** wildcard/apex TTL is 300 s; repoint with one
  `pdnsutil replace-rrset` + `increase-serial` and the secondary follows
  on NOTIFY.
- **Box loss:** primary → `restore-zone.sh` into a fresh box (refuses
  overlay); secondary → re-provision + `create-secondary-zone`. Both →
  re-provision from scripts + restore; if IPs changed, re-render and
  re-apply the glue diff (operator-approved again).

## Outage behavior

Secondary answers alone for up to SOA expire (7 days) on primary loss;
certificate *issuance* (broker/API) is down while the primary is down —
the correct degradation, since renewals carry ~30 days of slack and
resolution is what must not fail. The relay data plane has zero runtime
dependency on these boxes.

## Tests

`tools/network/estate/tests/test_serve_delegation.py` (the four-record
diff, NS-aware append, +N/−0 verification) and
`tests/test_challenge_broker.py` (name boundary, atomic multi-value TXT,
TTL/value bounds, expiry ledger, reconcile, restart survival) — pure,
no network. Live proofs are the runbook's retained outputs.
