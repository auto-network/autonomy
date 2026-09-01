# serve.auto.network — the registry-served authoritative zone (auto-g1jxw)

**The DNS server IS the registry.** Answers derive from registry state,
not a zone file: the responder (`tools/network/registry/dns_responder.py`)
computes every answer at query time, challenge TXT values live in the
registry store, and the `answer_a` seam is where tunnel-aware
per-hostname answers (the auto-0zdky lease table) plug in when
multi-relay lands. It runs as its OWN process on the relay host — one
system, one codebase, one store, two crash domains — so a UDP/53 flood
or codec fault can never stall a parked tunnel. Anycast direction and
resolver-behavior constraints: `graph read 799c4597-ba9`; design of
record `graph://c880c5e6-8bd@4` §5.3–§5.4; checkpoint
`graph://bdacdfd0-ebc` (+ redesign comments).

| Piece | Where |
|---|---|
| Responder (pure query→response, resolver-behavior matrix) | `tools/network/registry/dns_responder.py` |
| DNS process (UDP+TCP 53, token buckets, loopback state cache) | `tools/network/registry/dns_server.py` |
| Challenge write path (bounded; the auto-bhs3c seam) | `tools/network/registry/dns_challenges.py` + `GET /v1/dns/zone-state` |
| systemd unit / firewall / verify | this directory |

Zone shape: SOA (MINIMUM 60 — negatives die fast in a growing
namespace), NS `ns1/ns2.auto.network` (out-of-zone: the future anycast
cutover re-points two parent A records and touches neither delegation
nor zone), every in-zone A answers the relay IP at TTL 60 (the future
failover/steering plane), `_acme-challenge.<label>` TXT from the store,
CHAOS `id.server` reports the node id (the per-PoP identifier). ANY and
transfers REFUSED; answers minimal (amplification ≈ 1); RA always 0.
DNSSEC explicitly unsigned, no DS anywhere — answers are computed at
query time, so signing, if ever, is online signing by construction.

## Runbook — order matters, each step gated on the previous proof

```bash
# 0. From a host session holding the estate token + SSH key.
cd tools/network/estate

# 1. Ship the registry code that carries the responder (ordinary deploy):
../registry/deploy/deploy.sh root@5.161.219.195

# 2. Open 53 and start the DNS process on the relay host.
#    firewall-estate only allows TCP 22/80/443 + ICMP; this adds
#    UDP/53 + TCP/53 for the relay host. Only the primary IP answers
#    (the DNS unit binds it explicitly) — coturn's second IP untouched.
./dns/ensure-dns-firewall.sh                  # registry-ash-1 by default
./dns/deploy.sh --host root@5.161.219.195 --node-id registry-ash-1

# 3. Staging proofs — no parent change yet. Retain the output.
./dns/verify-dns.sh 5.161.219.195

# 4. COMPATIBILITY GATE — Zonemaster's full RFC battery against the
#    live server in UNDELEGATED mode (from any docker-equipped machine),
#    BEFORE any public change. Gate: no ERROR/CRITICAL findings; hold
#    and investigate WARNINGs.
docker run --rm zonemaster/cli serve.auto.network \
    --ns ns1.auto.network/5.161.219.195 \
    --ns ns2.auto.network/5.161.219.195
#    (Optional deeper rung: an unbound instance stub-zoned to the server,
#    resolving through it — exercises a real validating resolver's
#    QNAME-minimization/EDNS/0x20 behavior end to end.)

# 5. OPERATOR APPROVAL GATE — render the exact 4-record parent diff:
#    (on auto-ash-1, the Namecheap-whitelisted host; both IPs are the
#    relay host today — ns2's A relocates with the second machine)
python3 namecheap_dns.py add-delegation \
    --primary-ip 5.161.219.195 --secondary-ip 5.161.219.195 --dry-run

# 6. Apply the approved delegation (same host):
python3 namecheap_dns.py add-delegation \
    --primary-ip 5.161.219.195 --secondary-ip 5.161.219.195
#    pre-change set saved verbatim to /var/backups/namecheap/auto.network.before.xml

# 7. Post-cutover proof + captured parent sets:
./dns/verify-dns.sh 5.161.219.195 --public
python3 namecheap_dns.py gethosts \
    --save /var/backups/namecheap/auto.network.after-delegation.xml
```

**Registrar propagation gap (observed live 2026-09-01, don't panic):** there is
a ~5-minute window between Namecheap's API confirming the write (`gethosts`
shows all 4 records instantly) and Namecheap's *own* authoritative
nameservers (`dns1/dns2.registrar-servers.com`) publishing the delegation.
During that window they return **NXDOMAIN with the `aa` flag set** — an
authoritative negative, not resolver caching — so a `--public` check run
immediately after apply can legitimately fail. Confirm it's the window and
not a real failure by watching the parent SOA serial bump between checks,
then retry `--public` a few minutes later; it resolves cleanly once their
NS publish.

The four parent records: `serve NS ns1.auto.network.`,
`serve NS ns2.auto.network.`, `ns1 A <relay-ip>`, `ns2 A <relay-ip>` —
glue-less (the NS names are ordinary records of the parent zone
Namecheap already serves). **Honest dependency:** Namecheap remains in
the `serve.*` resolution path (it serves the parent zone, including the
delegation and the ns1/ns2 A records) until the auto.network apex
itself moves to the registry authoritative — a later phase requiring a
registrar NS change. `serve.*` is NOT Namecheap-free yet. NS-set
diversity is cosmetic until a second responder machine exists — do not
count it as redundancy yet.

## Future: the anycast cutover (written now, executed later)

When the anycast prefix/ASN exists, the entire DNS cutover is three
parent-zone A-record edits — no delegation change, no zone change, no
resolver-visible transition:

1. **Before anything:** relocate ns2's A record to the first
   non-anycast second machine (RFC 2182 — one NS permanently outside
   the anycast cloud, so a routing/RPKI mistake can never darken the
   zone).
2. Lower the ns1/ns2 A-record TTLs 3600 → 300, at least one old TTL
   (an hour) before the move.
3. Re-point ns1's A record to the anycast prefix; restore TTLs.

**Catchment verification before trusting any multi-PoP announcement:**
CHAOS `id.server` already reports the per-PoP node id — run RIPE Atlas
queries for `id.server` and map probe → PoP. Expect a roughly 80/20
imbalance with two sites; that is normal BGP behavior, not a fault —
size every PoP for 100% of load.

## Rollback / outage

- **Parent:** re-apply the saved `auto.network.before.xml` via setHosts —
  removes exactly the four delegation records; resolvers forget within
  the 3600 s NS TTL (mostly uncached — the records are new).
- **Answers:** TTL 60 on all data answers; any answer-policy change is a
  registry code deploy (at will, proof after) and propagates within a
  minute.
- **Process death:** systemd restarts it (Restart=on-failure); the
  relay/registry are untouched (separate crash domain). Registry down →
  challenge TXT degrades to NODATA while static answers keep serving —
  issuance has ~30 days of slack; resolution must not fail.
- **State:** challenges live in `registry.db`, covered by the existing
  registry snapshot/backup tooling — no DNS-private state anywhere,
  which is also what keeps the later multi-PoP story (fleet-sync
  TABLE_POLICIES over registry tables) a non-event for DNS.

## Compatibility tiers (how we know it interoperates)

1. **Primary matrix** (`test_dns_responder.py`) — behavior against an
   independently written mini-codec.
2. **Differential** (`test_dns_differential.py`) — the same matrix
   re-driven through dnspython's strict codec (software we did not
   write); skip-gated on dnspython availability.
3. **Zonemaster undelegated** — runbook step 4: the registry-grade RFC
   compliance battery against the live server, pre-delegation.
4. **Public consumers** — runbook step 7's four independent recursive
   resolvers, then a Let's Encrypt STAGING DNS-01 issuance (the
   pickiest real consumer, multi-perspective validation) as the gate
   before auto-rvq3j touches production ACME.

## Tests

`tools/network/registry/tests/test_dns_responder.py` — the
resolver-behavior matrix (EDNS0/TC, QNAME-minimization NODATA, AAAA
NODATA, RFC 2308 negatives, case echo, REFUSED for ANY/AXFR/out-of-zone,
CHAOS id.server, truncation vs TCP) against an independent mini codec.
`tests/test_dns_challenges.py` — bounded write path end to end through
the real store and the zone-state endpoint.
`tools/network/estate/tests/test_serve_delegation.py` — the exact
4-record parent diff with the +N/−0 mail-record gates.
