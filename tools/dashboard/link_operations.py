"""One code path for a share-link publish or revoke (auto-fkhq0.10a).

Two entry points reach it, and nothing else differs between them:

- an agent's or CLI's request approved in the Central inbox
  (link_approval_central.py): the operator signs after the Grant;
- the operator acting in their own browser (link_operation_routes.py): the
  requester IS the decider, so there is no approval, only the review dialog
  the operator confirms before signing.

Both go through :func:`plan` (validation at creation, so a doomed request never
reaches the operator), :func:`verify` (today's local authority check over the
persona-signed TUNNEL envelope, against the FROZEN request, signed no earlier
than the request was granted or prepared) and :func:`execute` (today's
executors, once only, recorded in the machine-homed link-operation journal).

Security is unchanged from the legacy approval: the envelope is signed in the
operator's browser with the persona-certified org session key, verified here
against the org binding and the local role fold, and carried out over the
org's serving tunnel.

Residual (stated, not fixed; .10b owns it): the signed bytes are TUNNEL + the
control path + ts + payload, not the approval, so two operations with
byte-identical frozen payloads accept each other's envelope within
MAX_CLOCK_SKEW. Each is once-only, so the worst case is one link published
twice that the operator approved twice.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import threading
import time
from typing import Any

from tools.dashboard import link_approvals as links
from tools.graph import settings_ops
from tools.graph.schemas.link_operation import LINK_OPERATION_REVISION, LINK_OPERATION_SET_ID

logger = logging.getLogger(__name__)

PUBLISH, REVOKE = "publish", "revoke"
_KIND_OP = {"link_publish": PUBLISH, "link_revoke": REVOKE}
_REVOKE_FIELDS = {"org", "token"}


class LinkOperationError(RuntimeError):
    """A bounded refusal. ``code`` is public; ``detail`` is server-authored."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code)


def op_for_kind(kind: str) -> str:
    return _KIND_OP[kind]


# ── planning: validated and frozen before anyone reviews it ─────────────


def plan(op: str, request: Any) -> dict:
    """Validate and freeze one link operation, or raise LinkOperationError
    ("invalid_request", reason) with the reason the requester should see.

    Returns ``{request, staged, review}``. ``staged`` is the exact registry
    request the operator signs plus the binding it was built against
    (today's first-render freeze, done at creation). ``review`` is what the
    operator is shown.
    """
    if not isinstance(request, dict):
        raise LinkOperationError("invalid_request", "the request must be an object")
    request = copy.deepcopy(request)
    if op == PUBLISH:
        return _plan_publish(request)
    if op == REVOKE:
        return _plan_revoke(request)
    raise LinkOperationError("invalid_request", "unknown link operation")


def _binding_or_refuse(org: str | None) -> dict:
    binding, error = links._load_binding(org)
    if binding is not None:
        return binding
    if links._is_registerable_on_first_publish(org):
        raise LinkOperationError(
            "invalid_request",
            f"{org} is not registered with auto.network yet: register it from its "
            f"organization page (/orgs/{org}) first, then publish",
        )
    raise LinkOperationError("invalid_request", error or f"{org} has no auto.network binding")


def _snapshot(binding: dict) -> dict:
    return {k: binding[k] for k in ("org_uuid", "root_pub", "registry_url")}


def _plan_publish(request: dict) -> dict:
    try:
        request, _ = links.prepare_create("", request)
    except ValueError as exc:
        raise LinkOperationError("invalid_request", str(exc)) from None
    org = request.get("org")
    binding = _binding_or_refuse(org)
    target = links._resolve_target(request.get("target_type", ""),
                                   request.get("target_uuid", ""), org, request)
    if target.get("error"):
        raise LinkOperationError("invalid_request", target["error"])
    recipient, recipient_error = links._link_recipient(request)
    if recipient_error:
        raise LinkOperationError("invalid_request", recipient_error)
    meta = request.get("meta") or {}
    identities = links._approval_identities(org)
    staged = {
        "method": "POST", "path": "/v1/links",
        "registry_url": binding["registry_url"],
        "payload": links._registry_payload(request, binding),
        "binding": _snapshot(binding),
    }
    fixed = request.get("target_type") in ("org:join", "org:follow")
    review = {
        "op": PUBLISH,
        "org": org,
        "target_type": request.get("target_type"),
        "type_label": links._TYPE_LABELS.get(request.get("target_type", ""),
                                             request.get("target_type")),
        "target_title": target.get("title") or request.get("target_uuid"),
        "label": meta.get("label"),
        "ttl": meta.get("ttl"),
        "fixed_expiry": fixed,
        "absolute_expiry": request.get("expires_at"),
        "recipient": recipient,
        "acting_identity": identities["acting_identity"],
        "actor_display_name": identities["actor_identity"].get("display_name"),
    }
    return {"request": request, "staged": staged, "review": review}


def _plan_revoke(request: dict) -> dict:
    if set(request) - _REVOKE_FIELDS:
        raise LinkOperationError(
            "invalid_request", "a revoke accepts only org and token, not "
            + ", ".join(sorted(set(request) - _REVOKE_FIELDS)))
    org, token = request.get("org"), request.get("token")
    if not isinstance(org, str) or not org or not isinstance(token, str) \
            or not links._TOKEN_RE.match(token):
        raise LinkOperationError("invalid_request", "a revoke names an org and a link token")
    grant = links._cached_grant(token, org)
    if not grant or not grant.get("target_type"):
        raise LinkOperationError(
            "invalid_request",
            "cannot revoke this link: its grant is not in this dashboard's cache, so "
            "it can't be classified — refresh the link list and retry")
    binding = _binding_or_refuse(org)
    resolved = links._resolve_target(grant.get("target_type", ""),
                                     grant.get("target_uuid", ""), org)
    identities = links._approval_identities(org)
    staged = {
        "method": "DELETE", "path": f"/v1/links/{token}",
        "registry_url": binding["registry_url"], "payload": {},
        "binding": _snapshot(binding),
    }
    review = {
        "op": REVOKE,
        "org": org,
        "target_type": grant.get("target_type"),
        "type_label": links._TYPE_LABELS.get(grant.get("target_type", ""),
                                             grant.get("target_type")),
        "target_title": resolved.get("title") or grant.get("target_uuid"),
        "label": (grant.get("meta") or {}).get("label"),
        "fixed_expiry": True,
        "recipient": None,
        "acting_identity": identities["acting_identity"],
        "actor_display_name": identities["actor_identity"].get("display_name"),
    }
    return {"request": request, "staged": staged, "review": review}


def signing_view(request: dict, staged: dict) -> dict:
    """What the review dialog signs and shows, read fresh (the target preview
    and any binding drift are live; the payload is the frozen one)."""
    org = request.get("org")
    binding, binding_error = links._load_binding(org)
    drift = binding is not None and links._binding_drift_error(staged, binding) is not None
    view = {
        "registry_request": {k: staged[k] for k in ("method", "path", "registry_url", "payload")},
        "org_uuid": (staged.get("binding") or {}).get("org_uuid"),
        "binding_drift": drift,
        "blocking_error": binding_error,
        "target_preview": None,
    }
    if staged.get("method") == "POST":
        target = links._resolve_target(request.get("target_type", ""),
                                       request.get("target_uuid", ""), org, request)
        view["target_preview"] = target.get("preview")
    return view


# ── verification: today's local authority, against the frozen request ────


def verify(op: str, request: dict, staged: dict, body: Any, *, not_before: float) -> tuple[dict, str]:
    """Check one signed operation body; return (decision, persona_pub).

    ``body`` is exactly ``{"envelope": ...}`` or, for a plain share link,
    ``{"envelope": ..., "ttl": int | None}``. Refusals are LinkOperationError
    ("invalid_request" | "stale_envelope" | "authority_refused" with the
    verifier's own words).
    """
    allowed = {"envelope"} | ({"ttl"} if op == PUBLISH and not _fixed(request) else set())
    if not isinstance(body, dict) or "envelope" not in body or set(body) - allowed:
        raise LinkOperationError("invalid_request")
    decision = {"approved": True, **body}
    envelope, subject, error = links._envelope_and_subject(decision)
    if error:
        raise LinkOperationError("authority_refused", error)
    ts = envelope.get("ts")
    if type(ts) is not int or ts < int(not_before):
        raise LinkOperationError("stale_envelope")
    org = request.get("org")
    binding, binding_error = links._load_binding(org)
    if binding_error or binding is None:
        raise LinkOperationError("authority_refused", binding_error or "the org has no binding")
    drift = links._binding_drift_error(staged, binding)
    if drift:
        raise LinkOperationError("authority_refused", drift)
    scope, pop = (("link:publish", links._TUNNEL_POP_PATH) if op == PUBLISH
                  else ("link:revoke", links._TUNNEL_REVOKE_POP_PATH))
    refusal = links._verify_local_publish_authority(envelope, subject, org, binding, scope, pop)
    if refusal:
        raise LinkOperationError("authority_refused", refusal)
    if op == PUBLISH:
        signed, payload_error = links._publish_payload_for_decision(staged, decision)
        if payload_error:
            raise LinkOperationError("authority_refused", payload_error)
        if envelope.get("payload") != signed:
            raise LinkOperationError(
                "authority_refused",
                "the signed payload does not match this request; review it and sign again")
    elif envelope.get("payload") != {}:
        raise LinkOperationError("authority_refused", "a revoke signs an empty payload")
    return decision, str(subject.get("id") or "")


def _fixed(request: dict) -> bool:
    return request.get("target_type") in ("org:join", "org:follow")


# ── the once-only journal ─────────────────────────────────────────────────


def _grant_id_for(key: str) -> str:
    """The grant id a publish writes first (O-C), fixed by the journal key so
    an interrupted claim can find its row."""
    return hashlib.sha256(f"autonomy.link.operation-grant\n{key}".encode()).hexdigest()[:32]


class Journal:
    """Read and write ``autonomy.machine.link-operation`` rows (machine-homed)."""

    @staticmethod
    def get(key: str) -> dict | None:
        row = settings_ops.read_set_key(LINK_OPERATION_SET_ID, key, org="machine", peers=[])
        payload = (row or {}).get("payload")
        return dict(payload) if isinstance(payload, dict) else None

    @staticmethod
    def put(key: str, payload: dict) -> None:
        settings_ops.write_by_key(LINK_OPERATION_SET_ID, LINK_OPERATION_REVISION, key,
                                  {k: v for k, v in payload.items() if v is not None},
                                  org="machine")

    @staticmethod
    def entries() -> list[tuple[str, dict]]:
        """Every journal entry on this machine, ``(key, payload)``, as stored:
        a claim left by a stopped process is not settled here (that is
        :func:`read`, on the operation's own path)."""
        rows = settings_ops.read_owned_set(LINK_OPERATION_SET_ID, org="machine",
                                           target_revision=LINK_OPERATION_REVISION)
        return [(str(m.key), dict(m.payload)) for m in rows.members
                if isinstance(m.payload, dict)]

    @staticmethod
    def stale_prepared(older_than: float) -> list[str]:
        """Operator operations prepared before *older_than* and never signed."""
        rows = settings_ops.read_owned_set(LINK_OPERATION_SET_ID, org="machine",
                                           target_revision=LINK_OPERATION_REVISION)
        return [m.key for m in rows.members
                if isinstance(m.payload, dict) and m.payload.get("state") == "prepared"
                and m.payload.get("initiator") == "operator"
                and float(m.payload.get("prepared_at") or 0) < older_than]

    @staticmethod
    def delete(key: str) -> None:
        row = settings_ops.read_set_key(LINK_OPERATION_SET_ID, key, org="machine", peers=[])
        if row and row.get("id"):
            settings_ops.remove_setting(row["id"], org="machine")


_lock = threading.Lock()
#: The longest one operation can hold its claim: the tunnel start and
#: create-link retries (60 s), the serving probe and compensation, with margin.
#: A claim older than this is settled even if its owner still lives.
CLAIM_HARD_BOUND_SECONDS = 600


def _owner() -> tuple[int, str | None]:
    from tools.dashboard.connector_key_resolution import process_start
    pid = os.getpid()
    return pid, process_start(pid)


def _owner_alive(entry: dict) -> bool:
    """Whether the process that claimed this entry still runs. Its kernel
    birth identity is compared, so a reused pid is not the owner."""
    from tools.dashboard.connector_key_resolution import process_start
    pid, start = entry.get("owner_pid"), entry.get("owner_start")
    if not isinstance(pid, int) or not isinstance(start, str):
        return False
    return process_start(pid) == start


def prepare_entry(key: str, *, op: str, initiator: str, planned: dict, now: float) -> dict:
    entry = {"state": "prepared", "op": op, "initiator": initiator, "prepared_at": now,
             "request": planned["request"], "staged": planned["staged"]}
    Journal.put(key, entry)
    return entry


def read(key: str, journal: type = Journal) -> dict | None:
    """The journal entry, with a claim left by a stopped process resolved
    from the grant row it names (published → done; unpublished → cleaned up,
    failed; absent → failed). Deterministic; nothing is resent.

    A claim is settled only when its owner process is gone, or it is older
    than :data:`CLAIM_HARD_BOUND_SECONDS`. A live owner, which may be another
    worker during a hot-reload overlap, is still running it: its grant row
    is not dropped under it."""
    entry = journal.get(key)
    if entry is None or entry.get("state") != "claimed":
        return entry
    with _lock:
        entry = journal.get(key) or entry
        if entry.get("state") != "claimed":
            return entry
        age = time.time() - float(entry.get("claimed_at") or 0)
        if _owner_alive(entry) and age < CLAIM_HARD_BOUND_SECONDS:
            return entry
        resolved = _resolve_interrupted(entry)
        entry = {**entry, **resolved, "finished_at": time.time()}
        journal.put(key, entry)
        return entry


def _resolve_interrupted(entry: dict) -> dict:
    request = entry.get("request") or {}
    org = request.get("org")
    if entry.get("op") == REVOKE:
        if links._cached_grant(request.get("token", ""), org) is None:
            return {"state": "done", "execution": {
                "ok": True, "token": request.get("token"), "via": "tunnel-control",
                "cache_removed": True}}
        return {"state": "failed", "execution": {
            "ok": False, "error": "the revoke was interrupted before it completed; revoke again"}}
    grant_id = entry.get("grant_id")
    grant = _grant_row(grant_id, org) if grant_id else None
    if grant is not None and links.is_published_grant(grant):
        from tools.dashboard.link_channel_key import fragment_url
        url = grant["url"]
        if grant.get("channel_pub") and grant.get("target_type") != "org:join":
            url = fragment_url(url, grant["channel_pub"])
        return {"state": "done", "execution": {
            "ok": True, "url": url, "channel_pub": grant.get("channel_pub"),
            "token": grant["token"], "serving": {"live": None, "via": "recovered"}}}
    if grant is not None:
        links._drop_cached_grant(grant_id, org)
    return {"state": "failed", "execution": {
        "ok": False, "error": "the publish was interrupted before the link was created"}}


def _grant_row(grant_id: str, org: str | None) -> dict | None:
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION, NETWORK_LINK_GRANT_SET_ID,
    )
    try:
        for m in settings_ops.read_owned_set(NETWORK_LINK_GRANT_SET_ID, org=org,
                                             target_revision=NETWORK_LINK_GRANT_REVISION).members:
            if m.key == grant_id and isinstance(m.payload, dict):
                return m.payload
    except Exception:
        return None
    return None


async def execute(key: str, entry: dict, decision: dict, persona_pub: str,
                  journal: type = Journal) -> dict:
    """Carry out one verified operation once. Returns the recorded execution
    (a replay returns the first one); raises LinkOperationError("running")
    while another request carries it out."""
    op = entry["op"]
    with _lock:
        current = journal.get(key) or entry
        if current.get("state") in ("done", "failed"):
            return current["execution"]
        if current.get("state") == "claimed":
            raise LinkOperationError("running")
        grant_id = _grant_id_for(key) if op == PUBLISH else _revoke_grant_id(entry)
        owner_pid, owner_start = _owner()
        claimed = {**current, "state": "claimed", "claimed_at": time.time(),
                   "grant_id": grant_id, "persona_pub": persona_pub,
                   "owner_pid": owner_pid, "owner_start": owner_start}
        journal.put(key, claimed)
    row = {"id": key, "request": entry["request"], "staged": entry["staged"]}
    try:
        if op == PUBLISH:
            execution = await links._execute_link_publish(row, decision, grant_id=grant_id)
        else:
            execution = await links._execute_link_revoke(row, decision)
    except Exception as exc:
        logger.exception("link operation %s failed", key)
        execution = {"ok": False, "error": f"the link operation failed ({type(exc).__name__})"}
    finally:
        decision.pop("envelope", None)
    with _lock:
        state = "done" if execution.get("ok") else "failed"
        journal.put(key, {**claimed, "state": state, "finished_at": time.time(),
                          "execution": execution})
    return execution


def _revoke_grant_id(entry: dict) -> str | None:
    request = entry.get("request") or {}
    grant = links._cached_grant(request.get("token", ""), request.get("org"))
    return (grant or {}).get("grant_id") or request.get("token")
