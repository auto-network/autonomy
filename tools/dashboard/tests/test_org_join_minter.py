"""Live authorized org:join grant minting through the share-link client.

The CLI (``graph link publish --target-type org:join``) posts the approval,
the fixture plays the operator (sign the tunnel PoP bytes, approve), and the
executor publishes the grant as a create-link control frame on the org's
authenticated serving tunnel (auto-qol1v — the HTTP mint is retired). The
tunnel control seam is stubbed with a recorder, so every byte that would
cross to the untrusted relay is captured: the invitation BEARER must never
be among them — only invite_ref and the invitation-aligned expiry cross.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth, approvals_routes, attention_routes
from tools.dashboard import link_approval_central as link_central
from tools.dashboard.approval_http_bridge import ApprovalHttpBridge, ApprovalHttpRegistry
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard import link_operations as link_ops
from tools.dashboard.link_operations import LinkOperationError
from tools.graph import link_cmd, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, derive_persona, issue_cert
from tools.network.invitation import decode_invitation
from tools.network.ledger import HLC, LedgerStore, make_event, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger
from tools.network.registry.signing import sign_request


ORG = "join-minter"
ORG_UUID = "019c0000-0000-7000-8000-000000000501"
REGISTRY_URL = "http://registry.test"
PUBLIC_LINK_URL = "https://relay.auto.network"
ROOT_SEED = bytes(range(32))
FOUNDER_SEED = bytes(range(32, 64))
OUTSIDER_SEED = bytes(range(64, 96))
INVITE_TOKEN = "ef" * 32
GRANT_TOKEN = "f00dfeed" * 4  # 32 lowercase hex, matches _TOKEN_RE
CHANNEL_PUB = "9a" * 32  # the per-link channel PUBLIC key the mint returns


class _MemoryJournal:
    """Link-operation journal held in memory: the machine-homed journal is
    the executor's own concern, and writing it here would touch node state."""

    rows: dict = {}

    @classmethod
    def get(cls, key):
        row = cls.rows.get(key)
        return dict(row) if row is not None else None

    @classmethod
    def put(cls, key, payload):
        cls.rows[key] = dict(payload)


def _args(invite_ref):
    return argparse.Namespace(
        target=invite_ref,
        target_type="org:join",
        ttl=None,
        label="Member invitation",
        org=ORG,
        invite_token_fd=None,
    )


def test_authorized_client_mints_exact_expiry_join_link_without_bearer_leak(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("AUTONOMY_INVITE_TOKEN", INVITE_TOKEN)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()

    now_ms = int(time.time() * 1000)
    invite_expiry = now_ms + 60_000
    root = KeyPair.from_private_hex(ROOT_SEED.hex())
    path = org_ledger_db_path(ORG)
    with LedgerStore(path) as store:
        founded = found_org_ledger(
            store,
            org_id=ORG_UUID,
            org_root=root,
            personal_root_seed=FOUNDER_SEED,
            now=now_ms - 60_000,
        )
        founder = derive_persona(FOUNDER_SEED, founded.genesis_id)
        last_hlc = max(store.get(head).hlc for head in store.heads())
        invite = make_event(
            founder,
            {
                "type": "invite",
                "granted_role": "owner",
                "expiry": invite_expiry,
                "sponsor": founder.public_hex,
                "token_hash": hashlib.sha256(
                    INVITE_TOKEN.encode("utf-8")
                ).hexdigest(),
            },
            store.heads(),
            HLC(last_hlc.ts, last_hlc.count + 1),
        )
        store.append(invite)

    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )

    # Tunnel seams (auto-qol1v): serving credential present (the pre-sign
    # guard), supervisor starts, and the control op is a recorder that mints
    # a well-formed grant echoing the invitation-aligned expiry.
    import tools.dashboard.link_serving_supervisor as _sup_mod

    control_calls = []

    class _TunnelStub:
        def start(self, org):
            return {"running": True}

    def _control_stub(org, op, args, *, timeout=12.0):
        control_calls.append((org, op, json.loads(json.dumps(args))))
        if op == "create-link":
            reply = {"ok": True, "token": GRANT_TOKEN,
                     "url": f"{PUBLIC_LINK_URL}/l/{GRANT_TOKEN}"}
            if "expires_at" in args:
                reply["expires_at"] = args["expires_at"]
            return reply
        return {"ok": False, "error": f"unexpected control op {op}"}

    monkeypatch.setattr(_sup_mod, "get_supervisor", lambda: _TunnelStub())
    monkeypatch.setattr(_sup_mod, "control", _control_stub)
    monkeypatch.setattr(_sup_mod, "serve_cert_state",
                        lambda org, **k: {"status": "ok"})

    # The per-link channel key is minted into the org-vaulted set; the vault
    # sealing itself is proven by its own suite, so stub the mint to return a
    # known public key. This lets the CLI assemble the complete two-value
    # #k=..&t=.. invitation URL deterministically (graph://4f9e881c-a9 §3).
    import tools.dashboard.link_channel_key as _lck_mod
    monkeypatch.setattr(_lck_mod, "mint_channel_key", lambda token, org: CHANNEL_PUB)

    # Publish ends with an end-to-end recipient probe over the relay (added
    # after this test was written); it has its own suite, and this test is
    # about the minting contract, so the probe is a live recorder. The
    # publish must still go through it.
    import tools.dashboard.link_approvals as _approvals_mod
    probes = []

    async def _probe_stub(binding, token, org, *, grant_id=None):
        probes.append((org, token))
        return {"live": True, "status": 200, "content_length": 0, "detail": "stub"}

    monkeypatch.setattr(_approvals_mod, "_probe_serving", _probe_stub)

    session_key = KeyPair.generate()

    def session_cert(persona):
        """Persona-signed session certificate (sign-on is a personal act):
        the local publish gate anchors the chain at the ACTING PERSONA in
        subject.id — a root-signed certificate is refused at hop 1."""
        return issue_cert(
            persona,
            session_key.public_hex,
            scope=("link:publish",),
            org=ORG_UUID,
            subject=Subject("operator", persona.public_hex),
            not_before=int(time.time()) - 60,
            not_after=int(time.time()) + 86_400,
        )

    active_cert = [session_cert(founder)]
    approval_requests = []

    # Central composition for the two link kinds (77c94ff1): the REAL planner,
    # persona verifier and executor; only the machine-local journal is kept in
    # memory, and the result destination is fixed to this machine.
    _MemoryJournal.rows = {}
    here = "h" * 43
    operator_root = KeyPair.generate()
    runtimes = {
        kind: link_central.build_approval_runtime(
            kind, destination_resolver=lambda: here, machine_label=lambda: "Home")
        for kind in link_central.KINDS
    }
    registry = build_production_registry(runtimes=runtimes)
    approvals = ApprovalService(
        registry=registry,
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: operator_root.public_hex,
        session_label_resolver=lambda subject: f"{subject} · Publishing",
    )
    desk = link_central.LinkApprovalDesk(
        approvals=approvals, destination_resolver=lambda: here, journal=_MemoryJournal)
    bridge = ApprovalHttpBridge(
        approvals=approvals,
        registry=ApprovalHttpRegistry(
            approvals=registry,
            adapters={kind: link_central.build_http_adapter(kind, desk)
                      for kind in link_central.KINDS},
        ),
    )
    monkeypatch.setattr(attention_routes, "_runtime",
                        SimpleNamespace(approval_http=bridge))
    # The requester is this org's agent session.
    requester = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject="auto-minter", org=ORG)
    monkeypatch.setattr(api_auth, "principal_from_request", lambda request: requester)

    def operate(approval_id):
        """The operator: review the frozen registry request, grant, then sign
        the fixed tunnel PoP bytes (worktrees.js) and carry the operation out
        on this machine. Publish rides the org tunnel, never a registry HTTP
        route."""
        registry_request = desk.bootstrap(approval_id)["registry_request"]
        # What the operator reviews: the staged org:join identity law
        # (org == target_uuid == the binding, exact invitation expiry).
        assert registry_request["payload"] == {
            "org": ORG_UUID,
            "target_uuid": ORG_UUID,
            "target_type": "org:join",
            "invite_ref": invite.event_id,
            "expires_at": invite_expiry,
            "meta": {"label": "Member invitation"},
        }
        approvals.decide(approval_id, HumanApprovalActor._verified(operator_root.public_hex),
                         outcome="granted", decision={})
        envelope = sign_request(
            session_key,
            "TUNNEL",
            "/control/create-link",
            registry_request["payload"],
            ts=int(time.time()) + 1,
            cert=active_cert[0],
        )
        return asyncio.run(desk.operate(approval_id, {"envelope": envelope}))

    with TestClient(Starlette(routes=approvals_routes.ROUTES)) as dashboard:
        def fake_api(method, route, *, body=None, timeout=None):
            if body is not None:
                approval_requests.append(json.loads(json.dumps(body)))
            response = dashboard.request(method, route, json=body)
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    route,
                    response.status_code,
                    "error",
                    {},
                    io.BytesIO(response.content),
                )
            result = response.json() if response.content else {}
            if method == "POST" and route == "/api/approvals":
                operate(result["id"])
            return result

        monkeypatch.setattr(link_cmd, "_api_request", fake_api)
        missing_ref = "00" * 32
        with pytest.raises(SystemExit):
            link_cmd.cmd_link_publish(_args(missing_ref))
        missing_cli = capsys.readouterr()
        assert "is not in" in missing_cli.err
        assert "authority ledger" in missing_cli.err
        assert "Traceback" not in missing_cli.err
        assert approval_requests == []

        # A request that bypasses the CLI is refused by the Central planner
        # before any approval exists. Over HTTP the requester gets only the
        # code: the link planner raises plain ValueError, which Central keeps
        # opaque (only ApprovalRequestRefused carries a detail, auto-gf08k).
        # The planner's own reason is pinned at its seam.
        missing_request = {
            "target_uuid": ORG_UUID,
            "target_type": "org:join",
            "invite_ref": missing_ref,
            "expires_at": invite_expiry,
            "meta": {},
        }
        missing_dashboard = dashboard.post(
            "/api/approvals",
            json={"kind": "link_publish",
                  "request": {"org_slug": ORG, **missing_request}},
        )
        assert missing_dashboard.status_code == 400
        assert missing_dashboard.json() == {"error": "invalid_request"}
        with pytest.raises(LinkOperationError) as missing_plan:
            link_ops.plan(link_ops.PUBLISH, {"org": ORG, **missing_request})
        assert missing_plan.value.detail == "invite_ref is not in the organization ledger"

        monkeypatch.setenv("AUTONOMY_INVITE_TOKEN", "wrong-bearer")
        with pytest.raises(SystemExit):
            link_cmd.cmd_link_publish(_args(invite.event_id))
        refused_token = capsys.readouterr()
        assert "does not match that invite" in refused_token.err
        assert approval_requests == []

        monkeypatch.setenv("AUTONOMY_INVITE_TOKEN", INVITE_TOKEN)
        link_cmd.cmd_link_publish(_args(invite.event_id))
        output = capsys.readouterr().out

        # The invitation bearer remains exclusively in the CLI's final
        # fragment. It is absent from the persisted approval request and
        # from every control frame that would cross to the untrusted relay.
        assert INVITE_TOKEN not in json.dumps(approval_requests)
        assert INVITE_TOKEN not in json.dumps(control_calls)

        # Exactly one create-link crossed, carrying the invitation binding
        # as top-level args (never meta) and the binding-reconciled uuid.
        org, op, wire_args = control_calls[-1]
        assert (org, op) == (ORG, "create-link")
        assert wire_args["target_uuid"] == ORG_UUID
        assert wire_args["target_type"] == "org:join"
        assert wire_args["invite_ref"] == invite.event_id
        assert wire_args["expires_at"] == invite_expiry
        assert wire_args.get("meta") == {"label": "Member invitation"}

        published_line = next(
            line for line in output.splitlines()
            if line.startswith("✓ share-link published: ")
        )
        join_url = published_line.split(": ", 1)[1]
        parsed = urllib.parse.urlsplit(join_url)
        assert parsed.scheme == "https"
        # The complete invitation URL carries BOTH independent fragment values:
        # the channel key k and the bearer t (graph://4f9e881c-a9 §3).
        from tools.network.invitation import encode_channel_pub
        assert urllib.parse.parse_qs(parsed.fragment) == {
            "k": [encode_channel_pub(CHANNEL_PUB)],
            "t": [INVITE_TOKEN],
        }
        grant_token = parsed.path.rsplit("/", 1)[-1]
        assert grant_token == GRANT_TOKEN
        # The published link was probed exactly once, for this org and token.
        assert probes == [(ORG, GRANT_TOKEN)]
        invite_code_line = next(
            line for line in output.splitlines()
            if line.startswith("  AUTONOMY_INVITE: ")
        )
        invitation = decode_invitation(invite_code_line.split(": ", 1)[1])
        assert invitation.org == ORG_UUID
        assert invitation.channel_pub == CHANNEL_PUB
        assert invitation.invite_ref == invite.event_id
        assert invitation.channel_token == grant_token
        assert invitation.claim_token == INVITE_TOKEN
        assert invitation.channel_token != invitation.claim_token

        cached = settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=ORG,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members
        # Since 1ed46d1f the grant row is keyed by a publisher-minted grant id
        # and carries the token in its payload (link_approvals.py).
        member = next(m for m in cached if m.payload.get("token") == grant_token)
        assert member.key != grant_token
        grant = member.payload
        assert grant["invite_ref"] == invite.event_id
        assert "#" not in grant["url"]

        # Shortening the absolute expiry is refused before an approval row
        # is created, so no operator can accidentally sign a dead-early link.
        short = dashboard.post(
            "/api/approvals",
            json={
                "kind": "link_publish",
                "request": {
                    "org_slug": ORG,
                    "target_uuid": ORG_UUID,
                    "target_type": "org:join",
                    "invite_ref": invite.event_id,
                    "expires_at": invite_expiry - 1,
                    "meta": {},
                },
            },
        )
        assert short.status_code == 400
        assert short.json() == {"error": "invalid_request"}
        with pytest.raises(LinkOperationError) as short_plan:
            link_ops.plan(link_ops.PUBLISH, {
                "org": ORG, "target_uuid": ORG_UUID, "target_type": "org:join",
                "invite_ref": invite.event_id, "expires_at": invite_expiry - 1,
                "meta": {},
            })
        assert "must equal the invitation expiry" in (short_plan.value.detail or "")

        # A non-member persona signs a perfectly valid chain to ITSELF, but
        # the local ledger authorize(link:publish) gate refuses the operation
        # before any control frame is emitted. Since 77c94ff1 that refusal is
        # the operator's (the operation step), not the requester's.
        outsider = derive_persona(OUTSIDER_SEED, founded.genesis_id)
        active_cert[0] = session_cert(outsider)
        crossed_before = len(control_calls)
        created = dashboard.post(
            "/api/approvals",
            json={"kind": "link_publish", "request": {
                "org_slug": ORG, "target_uuid": ORG_UUID, "target_type": "org:join",
                "invite_ref": invite.event_id, "expires_at": invite_expiry,
                "meta": {"label": "Member invitation"},
            }},
        )
        assert created.status_code == 200
        with pytest.raises(LinkOperationError) as refused:
            operate(created.json()["id"])
        assert refused.value.code == "authority_refused"
        assert "is not authorized to publish share links" in (refused.value.detail or "")
        assert len(control_calls) == crossed_before

    GraphDB.close_all_pooled()
