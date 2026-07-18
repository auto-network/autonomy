"""``graph link publish|revoke|list`` — the agent side of the share-link ceremony.

C3 of the auto.network share-link program (spec ``graph://a17c8657-939``
§6.4, §6.7). ``publish`` and ``revoke`` never touch the registry themselves:
they post an approval request of kind ``link_publish`` / ``link_revoke`` to
the dashboard's generalized approval primitive — the same rendezvous commit
signing uses — and block on the held GET until the operator decides. The
operator's browser renders WHAT is being shared, click-signs the registry
request with the operator session key, and the dashboard-side executor
returns the share URL through the approval result. A decline comes back as
a clean message, not an error dump.

``list`` reads the dashboard-side grant cache
(``autonomy.network.link-grant``), which the executor populates at issuance.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

from .duration import parse_duration

LINK_TARGET_TYPES = ("present", "design", "note", "file")

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_UUIDISH_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")

# One held GET per iteration; the server caps the hold at 60s.
_WAIT_SECONDS = 55
_HTTP_TIMEOUT = 70.0


def _fail(msg: str):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def _dash_base() -> str:
    return (
        os.environ.get("AUTONOMY_DASHBOARD")
        or os.environ.get("GRAPH_API")
        or "https://localhost:8080"
    )


def _api_request(method: str, path: str, *, body: dict | None = None,
                 timeout: float = _HTTP_TIMEOUT) -> dict:
    """One dashboard API call. Module-level so tests can swap the transport.

    Raises ``urllib.error.HTTPError`` for non-2xx (callers translate),
    ``urllib.error.URLError`` when the dashboard is unreachable.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # localhost self-signed, same as HttpClient
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_dash_base()}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _requesting_session() -> str:
    return os.environ.get("AUTONOMY_SESSION") or "cli"


def _resolve_org(args) -> str:
    org = getattr(args, "org", None) or os.environ.get("GRAPH_ORG")
    if not org:
        _fail("no org: pass --org or set GRAPH_ORG")
    return org


def _resolve_design_target(target_id: str, target_type: str) -> str:
    """Q5 trivial path: a Present deck / design must already exist in Design
    Studio ("shown") before it can be published; resolve the id (prefix ok)
    and error with guidance otherwise. Returns the stable design_id."""
    label = "Present deck" if target_type == "present" else "Design"
    try:
        design = _api_request("GET", f"/api/design/{target_id}/full")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _fail(
                f"{label} '{target_id}' is not in Design Studio, so there is "
                "nothing to serve for it. Create/show it first — e.g. "
                "`graph ui-design \"Title\" <dir>/` — then publish the id it prints."
            )
        _fail(f"could not resolve {label.lower()} '{target_id}': HTTP {e.code}")
    except urllib.error.URLError as e:
        _fail(f"cannot reach the dashboard at {_dash_base()}: {e.reason}")
    resolved = design.get("design_id") or design.get("id")
    if not resolved:
        _fail(f"dashboard returned no id for {label.lower()} '{target_id}'")
    title = design.get("title")
    if title:
        print(f"  target: {label} “{title}” ({resolved})")
    return resolved


def _await_decision(approval_id: str, verb: str) -> dict:
    """Block on the held GET until the operator decides; return the result.

    Mirrors the commit-sign shim's rendezvous semantics: an elapsed hold is
    re-held, transport hiccups back off briefly, no client-side timeout —
    the request waits until the operator acts (Ctrl-C to abandon)."""
    import time as _time
    while True:
        try:
            state = _api_request(
                "GET", f"/api/approvals/{approval_id}?wait={_WAIT_SECONDS}")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            _time.sleep(2)  # dashboard restart / network blip
            continue
        result = state.get("result")
        if result is None:
            continue  # hold elapsed undecided — hold a fresh one
        if not result.get("approved"):
            _fail(f"the operator declined the {verb} request — nothing was "
                  "published; confirm intent with them and retry if appropriate")
        execution = result.get("execution")
        if not isinstance(execution, dict):
            _fail(f"the {verb} was approved but the dashboard returned no "
                  "execution outcome (dashboard error)")
        if not execution.get("ok"):
            _fail(f"{verb} failed after approval: "
                  f"{execution.get('error', 'unknown error')}")
        return execution


def _post_approval(kind: str, request: dict) -> str:
    try:
        created = _api_request("POST", "/api/approvals", body={
            "kind": kind, "session": _requesting_session(), "request": request,
        })
    except urllib.error.HTTPError as e:
        _fail(f"the dashboard rejected the approval request: HTTP {e.code}")
    except urllib.error.URLError as e:
        _fail(f"cannot reach the dashboard at {_dash_base()}: {e.reason}")
    approval_id = created.get("id")
    if not approval_id:
        _fail("the dashboard did not return an approval id")
    return approval_id


def cmd_link_publish(args) -> None:
    """graph link publish <target-id> --type present|design|note|file
    [--ttl 7d] [--label text] [--org slug]"""
    org = _resolve_org(args)
    target_type = getattr(args, "target_type", None)
    if not target_type:
        _fail("--type is required: one of " + "|".join(LINK_TARGET_TYPES))
    target_id = args.target

    meta: dict = {}
    if getattr(args, "ttl", None):
        try:
            ttl = int(parse_duration(args.ttl))
        except (ValueError, TypeError):
            _fail(f"--ttl {args.ttl!r} is not a duration (try 3600, 24h, 7d)")
        if ttl <= 0:
            _fail("--ttl must be positive")
        meta["ttl"] = ttl
    if getattr(args, "label", None):
        meta["label"] = args.label

    if target_type in ("present", "design"):
        target_uuid = _resolve_design_target(target_id, target_type)
    else:
        if not _UUIDISH_RE.match(target_id):
            _fail(f"'{target_id}' does not look like a {target_type} id")
        target_uuid = target_id

    request = {"org": org, "target_uuid": target_uuid,
               "target_type": target_type, "meta": meta}
    approval_id = _post_approval("link_publish", request)
    print(f"⧗ share-link approval requested ({approval_id}) — waiting for the operator…")
    execution = _await_decision(approval_id, "share-link publish")
    print(f"✓ share-link published: {execution.get('url')}")
    print(f"  token: {execution.get('token')}")


def cmd_link_revoke(args) -> None:
    """graph link revoke <token> [--org slug]"""
    org = _resolve_org(args)
    token = args.target
    if not _TOKEN_RE.match(token):
        _fail("that is not a grant token (32 lowercase hex chars) — "
              "`graph link list` shows the tokens you hold")
    approval_id = _post_approval("link_revoke", {"org": org, "token": token})
    print(f"⧗ revoke approval requested ({approval_id}) — waiting for the operator…")
    execution = _await_decision(approval_id, "share-link revoke")
    print(f"✓ share-link revoked: {token}")
    if not execution.get("cache_removed", True):
        print("  (token was not in the local grant cache)")


def cmd_link_list(args) -> None:
    """graph link list [--org slug] — the local grant cache (issuance-fed)."""
    from .client import get_client
    from .schemas.network_identity import (
        NETWORK_BINDING_SET_ID,
        NETWORK_LINK_GRANT_SET_ID,
    )

    org = _resolve_org(args)
    client = get_client()
    members = list(client.read_set(NETWORK_LINK_GRANT_SET_ID, org=org))
    if not members:
        print("  no share-link grants cached for this org")
        return

    registry_url = None
    try:
        bindings = sorted(client.read_set(NETWORK_BINDING_SET_ID, org=org),
                          key=lambda m: m.key)
        if bindings:
            registry_url = (bindings[0].payload or {}).get("registry_url")
    except Exception:
        pass  # listing works without a binding; URLs just aren't printable

    for m in sorted(members, key=lambda m: (m.payload or {}).get("issued_at", "")):
        g = m.payload or {}
        meta = g.get("meta") or {}
        subject = g.get("subject") or {}
        url = f"{registry_url}/l/{g.get('token')}" if registry_url else g.get("token")
        bits = [g.get("target_type", "?"), g.get("target_uuid", "?")]
        if meta.get("label"):
            bits.append(f"“{meta['label']}”")
        if meta.get("ttl"):
            bits.append(f"ttl={meta['ttl']}s")
        bits.append(f"by {subject.get('kind', '?')}:{subject.get('id', '?')}")
        bits.append(g.get("issued_at", ""))
        print(f"  {url}")
        print(f"    {' · '.join(str(b) for b in bits)}")
