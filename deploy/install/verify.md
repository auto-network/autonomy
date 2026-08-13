# Claims and receipts

Every claim, one runnable receipt. Run these from the cloned checkout on
the user's machine; show the output, not this file. If a receipt fails,
that's a finding — report it instead of the claim.

## No CDN at runtime; works offline

**Claim:** every UI library is vendored in the image; browsers never
contact a third-party origin; a fresh install works offline.

```bash
python3 -m pytest tools/dashboard/tests/test_no_cdn_dependencies.py -q
curl -sk https://localhost:8080 | grep -i "content-security-policy" -A1 || \
  curl -skI https://localhost:8080 | grep -i content-security
ls tools/dashboard/static/vendor/
```

The test pins the property; the CSP names no third-party origin; the
vendor directory is the receipts in bytes.

## No phone-home, no account, no license check

**Claim:** at runtime the deployment contacts nothing you didn't configure.

```bash
grep -n "AUTONOMY_BASE_IMAGE\|AUTONOMY_TAILWIND_URL" docker-compose.yml
docker compose config | grep -iE "image:|environment:" -A8
```

The only external fetches are anonymous and build-time (base image, PyPI,
tailwind), each mirror-overridable. For the skeptical: watch the
container's egress with your tool of choice while using the dashboard.

## The image is signed; a tampered image is refused

**Claim:** the published image verifies against the project key; mutable
references are refused outright.

```bash
./deploy/verify-image.sh "some-repo/image:latest"     # exits 2: refuses mutable refs
./deploy/verify-image.sh "$AUTONOMY_IMAGE"            # image@sha256:... verifies or fails
```

## One volume is the whole deployment

**Claim:** move the volume, the machine is you — identity, orgs, graph,
everything.

```bash
docker volume inspect autonomy-data
python3 -m tools.portability snapshot --help
```

The snapshot/restore path is quiesced and fail-closed (refuses torn or
newer-format volumes). The full round-trip — snapshot A, restore into a
fresh C, C serves A's identity and content — is exercised by the project's
multi-node acceptance ladder (`python3 -m deploy.harness`, needs Docker
and ~10 minutes; offer, don't force).

## No mandated registry or distribution channel

**Claim:** you can obtain, build, and run Autonomy without permission from
anyone — including its authors.

```bash
grep -c "phone\|telemetry\|license" deploy/entrypoint.sh; cat deploy/entrypoint.sh
```

The receipt is the shape of the whole install you just did: a git clone
from a source the user chose and a local build. The served copy of this
very document is convenience, and you can diff it against
`deploy/install/verify.md` in the checkout.
