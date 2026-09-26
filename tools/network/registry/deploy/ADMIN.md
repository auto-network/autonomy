# Registry administration

Run from a checked-out repository on the **host terminal** with administrator
SSH access to the registry. These commands do not grant ordinary Autonomy
users allocation rights. The remote command uses administrator SSH. Root-run
commands execute through `systemd-run` as the existing `autonomy-registry`
DynamicUser with its StateDirectory; they do not create root-owned SQLite
sidecars. On-box access accepts root or the database service account. The reusable
allocation API is `tools.network.registry.domains.reserve_domain`; a future
commerce backend may call it only after authorizing the transaction.

## Deploy

Merge the reviewed changes to master and use that checkout. Keep the existing
real share-link smoke enabled; supply the link through the host's environment,
not a committed file:

```sh
python3 -m tools.network.registry.admin --target root@registry.auto.network deploy
```

This wraps `deploy.sh`, including import-closure preflight, service restart,
and the public smoke test. Set `SMOKE_LINK` before running it. Inspect
`https://registry.auto.network/versionz` for the deployed commit. Update the
dashboard to the same merged code before importing managed domains.
No database migration is required: the existing zone table stores the new
binding kind. On the development node, the schema and certificate modules use
the existing dashboard hot-reload path; verify the import API accepts the new
binding kind before publishing. No manual restart is required for these modules.

## Check a name

```sh
python3 -m tools.network.registry.admin --target root@registry.auto.network \
  domain show anchore.serve.auto.network
```

`"available": true` means neither an organization nor a member owns the name. This is a read-only snapshot,
not a hold. The reservation command checks again atomically. Reserved output
includes the owning `org_uuid` and state; even an inactive row is unavailable.
DNS resolution is **not** an availability check: the serving zone already
answers names before they have a published service.

## Reserve for an organization

Verify the organization's registry UUID against its authenticated dashboard
`GET /api/network/published-links` response (`org_uuid`). Do not substitute
the organization's display name, slug, persona key, or ledger genesis hash.
The allocation also refuses an unknown registry organization.

Anchore's operator-approved assignment:

```sh
python3 -m tools.network.registry.admin --target root@registry.auto.network \
  domain reserve anchore.serve.auto.network \
  --org c7d1f3a2-4c5e-4f60-8a71-9b2c3d4e5f60
```

Repeat the `domain show` command to verify the owner. A same-owner active
assignment is idempotent; an inactive same-owner assignment is reactivated.
A different owner or conflicting binding is refused without overwriting
ownership. No billing is performed.
No transfer or release command is included.

On-box equivalent, as root in `/opt/autonomy-registry`:

```sh
venv/bin/python -m tools.network.registry.admin \
  --db /var/lib/autonomy-registry/registry.db \
  domain show anchore.serve.auto.network
```

The database must already exist. Do not copy a live SQLite file or edit SQL by
hand; the library owns the transaction. See `BACKUP.md` for online snapshots.
Exit statuses: 0 success, 2 refused input/ownership/authority, 1 operational
failure. SSH/deploy failures propagate their exit status.

## Import the assignment into organization Settings

Using an operator-approved dashboard session, call the existing API in the
organization's context. This does **not** allocate the name: the relay checks
the authenticated organization against the administrator's existing record.

```sh
curl -sk --fail-with-body -b /tmp/dashboard-session.cookies \
  -H 'X-Graph-Org: anchore' -H 'Content-Type: application/json' \
  "$GRAPH_API/api/network/serve-zones" \
  --data '{"zone":"anchore.serve.auto.network","binding_kind":"registry"}'
```

Verify `GET /api/network/published-links` in the same org returns the zone as
active. Organization Settings → Published Links → Custom Domains lists it;
the existing publishing-domain picker can use it. No registrar changes or
external DNS ownership records are needed for a platform domain.

Publish an HTTP service from a session **in Anchore** as `hello`, selecting
`anchore.serve.auto.network`. The existing service flow obtains a certificate
for the domain and its wildcard, then registers the exact service hostname.

```sh
curl --fail --show-error https://hello.anchore.serve.auto.network/
```

Success requires HTTP 200, the intended Hello World content, and trusted TLS
(do not use `-k` for this proof). Retain the deployed commit, reservation
read-back, Settings read-back, public response and browser screenshot.
The demonstration is public; OIDC protection is a separate feature.

## Authority and failure behavior

Only the administrator CLI/library creates `binding_kind=registry` rows.
Ordinary `serve.zone.claim` requests can only import their own active row;
DNS-based claims cannot allocate platform names. Existing organization-domain
removal marks the domain inactive but retains its owner; it does not make the
name available to another organization. Host leases
and DNS-01 are checked against the assigned organization on the relay.

If publication fails, leave the assignment in place and inspect the existing
service status diagnostics. Stopping the Hello World service removes its
publication, not the domain reservation. This version deliberately has no
ownership transfer, deletion or automatic expiry operation.
