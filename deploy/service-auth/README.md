# Published Service authentication

## Activate for an organization

1. Open **Organization Settings → Published Services & Links → Services**.
   Add a Custom Domain first if none is active.
2. Under **Authentication**, choose **Set up OIDC → Okta**. Copy every displayed
   **Sign-in redirect URI** into the Okta application. For Anchore the value is:

   ```text
   https://*.anchore.serve.auto.network/oauth2/callback
   ```

3. In Okta Admin: **Applications → Applications → Create App Integration**.
   Choose **OIDC – OpenID Connect → Web Application**, name it **Autonomy**, and
   select **Authorization Code**. Under **Sign-in redirect URIs**, add the copied
   values and enable **Allow wildcard in sign-in redirect URI**. Under
   **Assignments**, assign the users or groups allowed to access these services.
   This is a private application: no catalog submission or Okta approval.
4. Back in Autonomy, enter the issuer (for example
   `https://your-tenant.okta.com`, without `-admin`), client ID and client
   secret. Choose whether new services default to **Org (OIDC)**, then Save.
5. On the published service card, choose **Access → Org (OIDC)**. Open its public
   address in a fresh browser and complete the Okta login. **Public** removes
   that service's gate. Changing the organization default never changes existing
   services; members can always override it.

One Okta application covers all listed domains and their services. No application
registration per member, container or service is needed. The **Other OIDC provider**
instructions describe Authorization Code, S256 PKCE, `form_post`, and
`openid email profile`; only Okta is validated by this integration's acceptance.
Wildcard support depends on the provider. Personal (passkey) is a separate provider
and is unavailable until its implementation is installed.

### Configuration and runtime state

`autonomy.network.service-auth#1`, key `default`, holds the organization's
`provider`, `issuer`, `client_id`, and `default_access` (`public` or `oidc`).
`autonomy.network.service-auth-secret#1`, key `default`, holds the client secret
in the existing organization audited vault. The setup API never returns it.
Existing `autonomy.network.service-target#1` rows carry the service's `access_mode`;
older rows without it remain public.

The existing gateway supervisor starts one lightweight oauth2-proxy helper per
organization on each serving node that has an OIDC-protected route. Helpers share
Caddy's private network namespace and have no published ports. The same supervisor
pass generates their Compose override and Caddy configuration from Settings.
Sorted helper IDs get loopback ports from 4180; helpers are recreated with a new
gateway instance. No relay or registry configuration change is needed.

The local cookie key is a `service-auth-cookie:<org>` row in
`autonomy.machine.vault.audited#1`. Secret files are materialized in the existing
`/run/autonomy-keycache/service-auth` memory-backed directory. Visitor sessions live
in encrypted host-only cookies, not a login database. The helper has no hard
resource limits; the earlier proof measured about 24 MiB RSS.

Implementation contract: graph://327842bf-6fb. Live acceptance is tracked by
auto-tmxuo; a passing unit suite alone does not establish delivery.

## Isolated proof harness

Run the authentication helper **beside Caddy on the serving node**, not at the
relay and not inside each application. The intended production unit is one
helper per organization per serving node, reused across that node's protected
Service hostnames. No central callback broker, Redis, or shared login database.

The standalone commands below run an **isolated proof**, not the production
supervisor path above. Its extra Caddy sits behind the existing node
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

- The standalone harness does not exercise the production Settings and supervisor
  path above. Mandatory organization policy is not part of this feature.
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
