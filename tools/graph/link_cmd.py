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

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .duration import parse_duration

LINK_TARGET_TYPES = ("present", "design", "note", "file", "org:join")

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
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


def _resolve_uuid_target(target_id: str, target_type: str) -> str:
    """Expand a note or attachment UUID prefix through the dashboard.

    Registry envelopes intentionally require full UUIDs. The graph CLI accepts
    the same unique-prefix ergonomics as other graph commands, so resolve the
    prefix before creating the approval request. Full UUIDs take the existing
    zero-round-trip path.
    """
    try:
        uuid.UUID(target_id)
        return target_id
    except (ValueError, AttributeError):
        pass
    if not _UUIDISH_RE.match(target_id):
        _fail(f"'{target_id}' does not look like a {target_type} id")

    endpoint = (
        f"/api/graph/source/{target_id}"
        if target_type == "note"
        else f"/api/graph/{target_id}"
    )
    try:
        resolved = _api_request("GET", endpoint)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _fail(f"could not resolve {target_type.lower()} '{target_id}'")
        _fail(f"could not resolve {target_type.lower()} '{target_id}': HTTP {e.code}")
    except urllib.error.URLError as e:
        _fail(f"cannot reach the dashboard at {_dash_base()}: {e.reason}")

    if not isinstance(resolved, dict):
        _fail(f"could not resolve {target_type.lower()} '{target_id}'")
    if target_type == "note":
        if resolved.get("type") not in (None, "note"):
            _fail(f"'{target_id}' resolves to {resolved.get('type')}, not a note")
    elif resolved.get("type") != "attachment":
        _fail(f"'{target_id}' resolves to {resolved.get('type', 'unknown')}, not a file")
    full_id = resolved.get("id")
    if not isinstance(full_id, str) or not _UUIDISH_RE.match(full_id):
        _fail(f"dashboard returned no full UUID for {target_type.lower()} '{target_id}'")
    return full_id


def _org_join_invite(org: str, invite_ref: str) -> dict:
    """Resolve the invitation from the org's verified local ledger."""
    from tools.network.ledger import (
        INVITE_LIVE,
        LedgerError,
        LedgerStore,
        org_ledger_db_path,
    )

    if not isinstance(invite_ref, str) or not _EVENT_ID_RE.fullmatch(invite_ref):
        _fail("org:join target must be a 64-char lowercase invite event id")
    path = org_ledger_db_path(org)
    if not path.exists():
        _fail(f"organization {org!r} has no founded authority ledger")
    try:
        with LedgerStore(path) as store:
            invite = store.get(invite_ref)
            if invite.type != "invite":
                _fail(f"{invite_ref} is not an invitation event")
            state = store.fold(now=int(time.time() * 1000))
            if state.invites.get(invite_ref) != INVITE_LIVE:
                _fail("that invitation is no longer live")
            genesis = store.get(store.ledger.genesis_id)
            return {
                "org_uuid": genesis.payload["org"],
                "root_pub": genesis.payload["root_pub"],
                "invite_ref": invite_ref,
                "expiry": invite.payload["expiry"],
                "token_hash": invite.payload.get("token_hash"),
            }
    except (KeyError, LedgerError):
        _fail(f"invitation {invite_ref} is not in {org!r}'s authority ledger")


def _invite_token(args, expected_hash: str | None) -> str:
    """Read the fragment bearer without ever placing it in an HTTP request."""
    fd = getattr(args, "invite_token_fd", None)
    if fd is not None:
        try:
            token = os.read(fd, 4096).decode("utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            _fail("could not read the invitation bearer from --invite-token-fd")
    else:
        token = os.environ.get("AUTONOMY_INVITE_TOKEN", "")
    if not token:
        _fail(
            "org:join requires the invitation bearer through "
            "--invite-token-fd or AUTONOMY_INVITE_TOKEN"
        )
    if len(token) > 128:
        _fail("the invitation bearer is malformed")
    if (
        not isinstance(expected_hash, str)
        or hashlib.sha256(token.encode("utf-8")).hexdigest() != expected_hash
    ):
        _fail("the supplied invitation bearer does not match that invite")
    return token


def _join_url(grant_url: str, invite_token: str) -> str:
    if not isinstance(grant_url, str):
        _fail("the registry returned no invitation grant URL")
    parsed = urllib.parse.urlsplit(grant_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        _fail("the registry returned a malformed invitation grant URL")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "t=" + urllib.parse.quote(
            invite_token,
            safe="",
        ))
    )


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
        if target_type == "org:join":
            _fail(
                "org:join lifetime is fixed to the invitation expiry; "
                "do not pass --ttl"
            )
        try:
            ttl = int(parse_duration(args.ttl))
        except (ValueError, TypeError):
            _fail(f"--ttl {args.ttl!r} is not a duration (try 3600, 24h, 7d)")
        if ttl <= 0:
            _fail("--ttl must be positive")
        meta["ttl"] = ttl
    if getattr(args, "label", None):
        meta["label"] = args.label

    invite_token = None
    invite_ref = None
    expires_at = None
    if target_type == "org:join":
        invite = _org_join_invite(org, target_id)
        invite_token = _invite_token(args, invite["token_hash"])
        target_uuid = invite["org_uuid"]
        invite_ref = invite["invite_ref"]
        expires_at = invite["expiry"]
    elif target_type in ("present", "design"):
        target_uuid = _resolve_design_target(target_id, target_type)
    else:
        target_uuid = _resolve_uuid_target(target_id, target_type)

    request = {"org": org, "target_uuid": target_uuid,
               "target_type": target_type, "meta": meta}
    if target_type == "org:join":
        request["invite_ref"] = invite_ref
        request["expires_at"] = expires_at
    approval_id = _post_approval("link_publish", request)
    print(f"⧗ share-link approval requested ({approval_id}) — waiting for the operator…")
    execution = _await_decision(approval_id, "share-link publish")
    url = execution.get("url")
    if target_type == "org:join":
        url = _join_url(url, invite_token)
    print(f"✓ share-link published: {url}")
    print(f"  token: {execution.get('token')}")
    # Agents relaying this URL have pasted it with adjacent text attached; the
    # operator's copy then picks up the trailing characters and the malformed
    # token 404s at the registry (2026-08-05: a trailing parenthetical arrived
    # as %0A%28…%29 and cost an investigation). Say so where it cannot be
    # missed rather than hoping each agent learns it the hard way.
    print("  Note to agents: give this URL to the operator bare, on its own "
          "line, with nothing before or after it — no wrapping text, no "
          "trailing punctuation or parentheses. Anything adjacent gets "
          "selected with the link and breaks it.")
    if target_type == "org:join":
        from tools.network.invitation import (
            encode_invitation,
            invitation_from_join_url,
        )

        invitation = invitation_from_join_url(
            org=invite["org_uuid"],
            root_pub=invite["root_pub"],
            invite_ref=invite["invite_ref"],
            join_url=url,
        )
        print(f"  AUTONOMY_INVITE: {encode_invitation(invitation)}")


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
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
    )

    org = _resolve_org(args)
    client = get_client()
    # Owning-scope (P2): 'graph link list' shows only THIS org's own grants;
    # a peer-published grant row must never appear.
    members = list(client.read_set(
        NETWORK_LINK_GRANT_SET_ID,
        org=org,
        peers=[],
        target_revision=NETWORK_LINK_GRANT_REVISION,
    ))
    if not members:
        print("  no share-link grants cached for this org")
        return

    for m in sorted(members, key=lambda m: (m.payload or {}).get("issued_at", "")):
        g = m.payload or {}
        meta = g.get("meta") or {}
        subject = g.get("subject") or {}
        url = g["url"]
        bits = [g.get("target_type", "?"), g.get("target_uuid", "?")]
        if meta.get("label"):
            bits.append(f"“{meta['label']}”")
        if meta.get("ttl"):
            bits.append(f"ttl={meta['ttl']}s")
        bits.append(f"by {subject.get('kind', '?')}:{subject.get('id', '?')}")
        bits.append(g.get("issued_at", ""))
        print(f"  {url}")
        print(f"    {' · '.join(str(b) for b in bits)}")
