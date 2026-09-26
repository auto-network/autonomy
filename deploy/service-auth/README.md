# Local OIDC gate proof

Run the authentication helper **beside Caddy on the serving node**, not at the
relay and not inside each application. The intended production unit is one
helper per organization per serving node, reused across that node's protected
Service hostnames. No central callback broker, Redis, or shared login database.

This directory is an **isolated proof**, not production organization-policy or
gateway-supervisor integration. Its extra Caddy sits behind the existing node
TLS gateway; the private example application only returns a marker. The relay
continues forwarding TLS ciphertext. TLS terminates on the serving node, not
inside the application container. Auth does not extend browser TLS through the
local HTTP hop to the application.

## Generate and run

Use a private Web OIDC registration, authorization-code grant, S256 PKCE, and
assigned users. Register callback URLs ending in `/oauth2/callback`; a provider-
supported wildcard may cover the organization's allocated Service hostnames.
Use the issuer URL, **not** an Okta `-admin` URL. No OIN/catalog submission is
needed. Store the client secret in an owner-only file under a private local
`/tmp` directory; never put it in shell arguments, source, graph, or artifacts.

From the repository root, as a nonroot user:

```sh
python3 -m tools.network.service_auth \
  --name a --host demo.example.org --host another.example.org \
  --issuer https://your-tenant.okta.com --client-id YOUR_CLIENT_ID \
  --port 8101 --client-secret-file /tmp/private-oidc/client-secret \
  --runtime-dir /tmp/private-oidc-runtime-a
docker compose -f /tmp/private-oidc-runtime-a/compose.json up -d
```

The runtime directory must not already exist. The generator creates independent
cookie keys and files with private permissions. This proof intentionally permits
only local `/tmp` runtime storage: its secrets and containers are not a persistent
production deployment. Publish the gateway port through the existing Service
reservation/target workflow, with the exact hostname supplied above. The plain
HTTP gateway port belongs on the private node/session network only, behind the
existing TLS gateway. The auth port is loopback-only in Caddy's network namespace;
the app cannot reach it. Caddy admin is off.

The pinned auth image is OAuth2 Proxy 7.15.4 (single Go binary, about 42.7 MiB
uncompressed image). It runs nonroot, read-only, without Linux capabilities and
with no-new-privileges. No hard resource limits are imposed; measured idle RSS
in the proof was about 24 MiB per helper. Go's soft memory target is 96 MiB with
one processor. Caddy alone retains NET_BIND_SERVICE because
the official binary's file capability otherwise prevents exec on this host.

## Request and state flow

1. Browser reaches the serving node's Caddy through the TLS relay.
2. Caddy asks the local helper whether the browser's cookie is valid. No valid
   cookie means redirect to the local login endpoint, then to the OIDC provider.
3. The provider authenticates the visitor and form-POSTs a code and state to
   `https://<the-requested-service>/oauth2/callback` through that same relay.
4. The local helper checks browser transaction binding, exchanges the code with
   PKCE, verifies the OIDC identity, and creates a local encrypted session cookie.
5. Browser returns to the Service root; Caddy's auth check now succeeds and it
   proxies the request to the application.

The callback is local to the originating serving instance. Each instance has its
own cookie key; hosts sharing an instance use host-only browser cookies. Okta
does not require a registration per app/container/member. Authorization in this
proof is the configured issuer's application assignment, **not** an email suffix
or an assertion that every Okta user belongs to an Autonomy organization.

Session cookies: Secure, HttpOnly, host-only, SameSite=Lax, 15-minute absolute
expiry, no refresh. Transaction cookies: five minutes, SameSite=None for the
cross-site form POST. Tokens are omitted from the minimized session cookie and
not passed to the app. Client-provided identity/authorization headers are stripped.
The helper's request/auth logging is disabled. `form_post` avoids putting the
returned authorization code/state in the existing TLS gateway's URI access log.

## Verification

```sh
agent-test run tools/network/tests/test_service_auth.py
```

For actual runtime proof, deploy independent A and B stacks plus host C sharing
A. Visit all three with an assigned real user. Export browser state to a private
local file (contains credentials!), chmod it 0600, then run:

```sh
python3 deploy/service-auth/prove.py \
  --browser-state /tmp/private-oidc/browser-state.json \
  --host-a a.example.org --host-b b.example.org --host-c c.example.org
```

The harness prints only sanitized checks, never cookies or OAuth URLs. It checks
anonymous/forged/tampered denial, live authenticated app markers, host-only cookie
flags, independent-key session rejection, PKCE/form-post parameters, and a login
transaction sent to the wrong instance without browser binding. The latter uses
an invalid test code: it is **not** proof of live authorization-code replay
rejection. Malformed state produces OAuth2 Proxy's 500; well-formed state without
the CSRF cookie produces 403. Neither may reach the app.

Also stop the B helper briefly and verify its published URL returns 502, then
restart it. Test an unconfigured Host against the inner gateway: expect 421.
Inspect the adapted Caddy JSON when changing auth responses. In particular,
`redir * /oauth2/start?... 302` requires the explicit `*`: without it Caddy treats
the leading-slash destination as a matcher and can fall through to the app!

## Boundaries and follow-up

- Organization opt-in/required policy, secret delivery, production supervisor
  lifecycle and dormant startup are not implemented here.
- Two instances on one host demonstrate isolated state, not two physical nodes.
- Local cookie sessions have no immediate revocation; existing sessions can
  survive assignment removal until their 15-minute expiry.
- Login returns to `/`; arbitrary deep-link restoration is deferred.
- This gate protects browser HTTP Services, not arbitrary TCP protocols.
- The 2026-09-26 proof observed cross-host browser navigation returning an outer
  gateway 502 while fresh connections worked. Cause remains unknown. Authentication
  succeeded on all three hosts after connection restart; this is not a clean
  multi-host browsing acceptance result and needs a separate relay/gateway check.

Design/provenance: graph://66fe94cc-c12. Tracking: auto-4sytl, auto-7ipua,
auto-i2q3b. The proof does not claim production readiness.
