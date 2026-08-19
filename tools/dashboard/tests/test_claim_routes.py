"""Live Node-to-org acceptance for the member-claim protocol."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette

from tools.dashboard import claim_service, network_routes
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import (
    sign_delegate_proof,
    Event,
    HLC,
    INVITE_CLAIMED,
    LedgerStore,
    make_event,
    org_ledger_db_path,
    sign_approval,
)
from tools.network.ledger.claims import mint_member_claim
from tools.network.ledger.found import found_org_ledger
from tools.network.storagekit.credentials import build as build_credential


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/claim-live.mjs"
)
ORG_ID = "019c0000-0000-7000-8000-000000000301"
ROOT_SEED = bytes(range(32))
FOUNDER_PERSONAL_SEED = bytes(reversed(range(32)))
INVITEE_PERSONAL_SEED = bytes(range(32, 64))
KEY_PERSONAL_SEED = bytes(range(64, 96))
OUTSIDER_PERSONAL_SEED = bytes(range(96, 128))
KEM_SEED = bytes(range(128, 160))
KEY_KEM_SEED = bytes(range(160, 192))
EXTRA_APPROVER_PERSONAL_SEED = bytes(range(224, 256))
TOKEN = "a1" * 32
WRONG_INVITE_TOKEN = "b2" * 32
EXPIRED_TOKEN = "c3" * 32
SELF_TOKEN = "e5" * 32


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port: int):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _run_node(tmp_path: Path, fixture: dict) -> dict:
    fixture_path = tmp_path / "claim-live.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        ["node", str(NODE_DRIVER), str(fixture_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_live_claim_pending_countersign_and_invitee_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orgs_dir = tmp_path / "orgs"
    slug = "claim-live"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", slug)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    database = GraphDB.create_org_db(
        slug,
        root=orgs_dir,
        org_id=ORG_ID,
    )
    database.close()

    wall_now = int(time.time() * 1000)
    founded_at = wall_now - 60_000
    root = KeyPair.from_private_hex(ROOT_SEED.hex())
    path = org_ledger_db_path(slug)
    with LedgerStore(path) as store:
        founded = found_org_ledger(
            store,
            org_id=ORG_ID,
            org_root=root,
            personal_root_seed=FOUNDER_PERSONAL_SEED,
            now=founded_at,
        )
        founder = derive_persona(FOUNDER_PERSONAL_SEED, founded.genesis_id)
        extra_approver = derive_persona(
            EXTRA_APPROVER_PERSONAL_SEED,
            founded.genesis_id,
        )
        next_ts = founded_at

        def emit(author: KeyPair, payload: dict) -> str:
            nonlocal next_ts
            next_ts += 1_000
            return store.append(
                make_event(
                    author,
                    payload,
                    store.heads(),
                    HLC(next_ts),
                )
            )

        emit(
            root,
            {
                "type": "role.define",
                "name": "member",
                "scope_set": ["link:publish"],
                "claim_requires": "admin-ack",
                "approver_threshold": {"kind": "static", "count": 2},
                "version": 1,
            },
        )
        emit(
            root,
            {
                "type": "delegate",
                "child_pub": extra_approver.public_hex,
                "scope": ["role:grant:member"],
                "can_redelegate": False,
                "proof": sign_delegate_proof(
                    extra_approver, founded.genesis_id,
                    root.public_hex, ["role:grant:member"],
                ),
            },
        )
        emit(
            root,
            {
                "type": "role.define",
                "name": "self-member",
                "scope_set": ["link:publish"],
                "claim_requires": "self",
                "version": 1,
            },
        )

        def bearer_invite(token: str, role: str, expiry: int) -> str:
            return emit(
                founder,
                {
                    "type": "invite",
                    "granted_role": role,
                    "expiry": expiry,
                    "sponsor": founder.public_hex,
                    "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                },
            )

        invite_ref = bearer_invite(TOKEN, "member", wall_now + 120_000)
        self_invite_ref = bearer_invite(
            SELF_TOKEN,
            "self-member",
            wall_now + 120_000,
        )
        wrong_invite_ref = bearer_invite(
            WRONG_INVITE_TOKEN,
            "member",
            wall_now + 120_000,
        )
        key_persona = derive_persona(KEY_PERSONAL_SEED, founded.genesis_id)
        key_invite_ref = emit(
            founder,
            {
                "type": "invite",
                "granted_role": "self-member",
                "expiry": wall_now + 120_000,
                "sponsor": founder.public_hex,
                "invite_pub": key_persona.public_hex,
            },
        )
        # Last setup head, but still far enough behind its wall-clock expiry
        # that a ticked claim HLC remains unexpired. The online TTL refusal
        # therefore proves it runs before the deterministic fold backstop.
        expired_invite_ref = bearer_invite(
            EXPIRED_TOKEN,
            "member",
            wall_now - 1_000,
        )
        setup_heads = list(store.heads())
        setup_max_hlc = max(store.get(head).hlc for head in setup_heads)
        assert setup_max_hlc.ts < wall_now - 1_000
        setup_events = len(store)

    fixture = {
        "org": slug,
        "genesis_id": founded.genesis_id,
        "root_seed_hex": ROOT_SEED.hex(),
        "root_pub": root.public_hex,
        "founder_personal_seed_hex": FOUNDER_PERSONAL_SEED.hex(),
        "personal_seed_hex": INVITEE_PERSONAL_SEED.hex(),
        "key_personal_seed_hex": KEY_PERSONAL_SEED.hex(),
        "outsider_personal_seed_hex": OUTSIDER_PERSONAL_SEED.hex(),
        "extra_approver_personal_seed_hex": EXTRA_APPROVER_PERSONAL_SEED.hex(),
        "kem_seed_hex": KEM_SEED.hex(),
        "key_kem_seed_hex": KEY_KEM_SEED.hex(),
        "invite_ref": invite_ref,
        "self_invite_ref": self_invite_ref,
        "key_invite_ref": key_invite_ref,
        "wrong_invite_ref": wrong_invite_ref,
        "expired_invite_ref": expired_invite_ref,
        "token": TOKEN,
        "self_token": SELF_TOKEN,
        "expired_token": EXPIRED_TOKEN,
        "profile": {"display_name": "Invitee", "team": "systems"},
        # Deliberately behind every org head: claim.js must tick max_hlc.
        "lagging_now_ms": founded_at,
    }

    app = Starlette(routes=network_routes.ROUTES)
    with _live_server(app, _free_port()) as server_url:
        fixture["server_url"] = server_url
        result = _run_node(tmp_path, fixture)

    # Context/HLC: a lagging invitee still mints strictly after its parents.
    assert result["keyContext"]["heads"] == setup_heads
    assert result["keyContext"]["maxHlc"] == setup_max_hlc.to_list()
    assert tuple(result["keyClaim"]["event"]["hlc"]) > (
        setup_max_hlc.ts,
        setup_max_hlc.count,
    )
    assert tuple(result["expired"]["event"]["hlc"]) > (
        setup_max_hlc.ts,
        setup_max_hlc.count,
    )
    assert result["expired"]["event"]["hlc"][0] < wall_now - 1_000

    # The wall clock is the primary expiry locus: an event-HLC-unexpired
    # claim is rejected before trial fold and leaves no staging row.
    assert result["expiredSubmit"] == {
        "status": 400,
        "body": {"status": "rejected", "reason": "invite-expired"},
    }
    assert result["expiredStatus"] == {"status": "absent"}

    token_self_position = {
        "parents": result["tokenSelfClaim"]["event"]["parents"],
        "hlc": result["tokenSelfClaim"]["event"]["hlc"],
    }
    initial_position = {
        "parents": result["initial"]["event"]["parents"],
        "hlc": result["initial"]["event"]["hlc"],
    }

    # Bearer safety overrides a role's otherwise self-admitting policy.
    assert result["tokenSelfSubmit"] == {
        "status": "pending",
        "have": 0,
        "need": 1,
    }
    assert result["tokenSelfStatus"] == {
        "status": "pending",
        "genesis_id": founded.genesis_id,
        "granted_role": "self-member",
        "have": 0,
        "need": 1,
        "approvals": [],
        "admitting": [],
        "position": token_self_position,
    }

    # Key binding + self policy admits immediately.
    assert result["keySubmit"] == {
        "status": "admitted",
        "kem_credential": result["keyClaim"]["kemCredential"],
    }
    assert result["keyStatus"] == {"status": "admitted"}

    # Bearer safety holds pending; approval traffic changes no ledger heads.
    assert result["pending"] == {"status": "pending", "have": 0, "need": 2}
    assert result["pendingStatus"] == {
        "status": "pending",
        "genesis_id": founded.genesis_id,
        "granted_role": "member",
        "have": 0,
        "need": 2,
        "approvals": [],
        "admitting": [],
        "position": initial_position,
    }
    assert result["headsAfterPending"] == result["bearerHeads"]
    assert result["badSignatureApproval"] == {
        "status": 400,
        "body": {
            "status": "rejected",
            "reason": "bad-signature",
        },
    }
    assert result["unauthorizedApproval"] == {
        "status": 403,
        "body": {
            "status": "rejected",
            "reason": "approver-not-authorized",
        },
    }
    assert result["afterRefusals"] == result["pendingStatus"]
    assert result["firstApproval"] == {
        "status": "pending",
        "have": 1,
        "need": 2,
        "position": initial_position,
    }
    assert result["headsAfterFirstApproval"] == result["bearerHeads"]
    assert result["secondApproval"] == {
        "status": "ready",
        "have": 2,
        "need": 2,
        "kem_credential": result["initial"]["kemCredential"],
        "admitting": sorted(
            [founder.public_hex, root.public_hex]
        ),
        "position": initial_position,
    }
    assert result["readyApproval"] == {
        "status": "ready",
        "have": 3,
        "need": 2,
        "kem_credential": result["initial"]["kemCredential"],
        "admitting": sorted(
            [
                founder.public_hex,
                root.public_hex,
                extra_approver.public_hex,
            ]
        )[:2],
        "position": initial_position,
    }
    assert "admitted" not in result["firstApproval"]
    assert "admitted" not in result["secondApproval"]
    assert "admitted" not in result["readyApproval"]
    assert result["headsAfterReadyApproval"] == result["bearerHeads"]
    assert result["readyStatus"]["status"] == "pending"
    assert result["readyStatus"]["have"] == 3
    assert result["readyStatus"]["need"] == 2
    assert result["finalApprovals"] == [
        entry
        for entry in result["readyStatus"]["approvals"]
        if entry["key"] in result["readyApproval"]["admitting"]
    ]
    assert (
        result["shiftedFinalizeHeads"]
        != result["readyApproval"]["position"]["parents"]
    )
    assert result["finalClaim"]["event"]["parents"] == initial_position["parents"]
    assert result["finalClaim"]["event"]["hlc"] == initial_position["hlc"]
    assert (
        result["finalClaim"]["kemCredential"]["authority_heads"]
        == initial_position["parents"]
    )
    assert (
        result["finalClaim"]["kemCredential"]["created_hlc"]
        == initial_position["hlc"]
    )
    assert result["mismatchedCredentialHlcRejected"] is True
    assert result["mismatchedSubmitPositionRejected"] is True

    # Only the invitee's re-signed final submit appends and clears staging.
    assert result["admitted"] == {
        "status": "admitted",
        "kem_credential": result["initial"]["kemCredential"],
    }
    assert result["admittedStatus"] == {"status": "admitted"}
    assert result["expiredStaleSubmit"] == result["expiredSubmit"]
    assert result["wrongToken"] == {
        "status": 403,
        "body": {"status": "rejected", "reason": "claim-bad-token"},
    }
    assert result["wrongStatus"] == {"status": "absent"}

    # Cross-language byte parity for credential + initial claim + approvals.
    persona = derive_persona(INVITEE_PERSONAL_SEED, founded.genesis_id)
    created_hlc = tuple(
        result["initial"]["kemCredential"]["created_hlc"]
    )
    credential, kem_private = build_credential(
        persona,
        founded.genesis_id,
        KEM_SEED,
        result["bearerHeads"],
        created_hlc,
    )
    assert credential.to_dict() == result["initial"]["kemCredential"]
    assert kem_private == result["initial"]["kemPrivateKey"]
    reference, _ = mint_member_claim(
        INVITEE_PERSONAL_SEED,
        founded.genesis_id,
        invite_ref=invite_ref,
        heads=result["bearerHeads"],
        hlc=HLC.from_value(result["initial"]["event"]["hlc"]),
        profile=fixture["profile"],
        token=TOKEN,
        kem_credential=credential.to_dict(),
    )
    assert reference.to_dict() == result["initial"]["event"]
    assert reference.to_json().decode("utf-8") == result["initial"]["wire"]
    assert sign_approval(
        founder,
        "member.claim",
        result["initial"]["event"]["payload"],
    ) == result["founderApproval"]
    assert sign_approval(
        root,
        "member.claim",
        result["initial"]["event"]["payload"],
    ) == result["rootApproval"]

    with LedgerStore(path) as store:
        assert len(store) == setup_events + 2
        assert store.db.execute(
            "SELECT COUNT(*) FROM ledger_pending_claims"
        ).fetchone()[0] == 1
        initial_id = Event.from_dict(result["initial"]["event"]).event_id
        token_self_id = Event.from_dict(
            result["tokenSelfClaim"]["event"]
        ).event_id
        final_id = Event.from_dict(result["finalClaim"]["event"]).event_id
        expired_id = Event.from_dict(result["expired"]["event"]).event_id
        wrong_id = Event.from_dict(result["wrong"]["event"]).event_id
        assert initial_id not in store
        assert token_self_id not in store
        assert final_id in store
        assert expired_id not in store
        assert wrong_id not in store
        state = store.fold(now=wall_now)
        assert state.members[persona.public_hex].roles == ("member",)
        assert state.members[key_persona.public_hex].roles == ("self-member",)
        assert state.invites[invite_ref] == INVITE_CLAIMED
        assert (
            state.members[persona.public_hex].kem_credential
            == result["initial"]["kemCredential"]
        )

    # Requires-aware gate: root/admin authority cannot substitute for the
    # invite sponsor when claim_requires is the sponsor branch.
    sponsor_token = "d4" * 32
    sponsor_seed = bytes(range(192, 224))
    sponsor_persona = derive_persona(sponsor_seed, founded.genesis_id)
    with LedgerStore(path) as store:
        max_hlc = max(store.get(head).hlc for head in store.heads())
        role_event = make_event(
            root,
            {
                "type": "role.define",
                "name": "sponsor-only",
                "scope_set": ["link:publish"],
                "claim_requires": "sponsor",
                "version": 1,
            },
            store.heads(),
            max_hlc.tick(max_hlc.ts),
        )
        store.append(role_event)
        invite_event = make_event(
            founder,
            {
                "type": "invite",
                "granted_role": "sponsor-only",
                "expiry": wall_now + 120_000,
                "sponsor": founder.public_hex,
                "token_hash": hashlib.sha256(
                    sponsor_token.encode("utf-8")
                ).hexdigest(),
            },
            store.heads(),
            role_event.hlc.tick(role_event.hlc.ts),
        )
        store.append(invite_event)
        sponsor_claim, _ = mint_member_claim(
            sponsor_seed,
            founded.genesis_id,
            invite_ref=invite_event.event_id,
            heads=store.heads(),
            hlc=invite_event.hlc.tick(invite_event.hlc.ts),
            token=sponsor_token,
        )
    assert claim_service.submit(slug, sponsor_claim.to_json()) == {
        "status": "pending",
        "have": 0,
        "need": 1,
    }
    claim_payload = sponsor_claim.payload
    root_entry = sign_approval(root, "member.claim", claim_payload)
    assert claim_service.countersign(
        slug,
        invite_event.event_id,
        sponsor_persona.public_hex,
        root_entry,
    ) == {
        "status": "rejected",
        "reason": "approver-not-authorized",
    }
    assert claim_service.status(
        slug,
        invite_event.event_id,
        sponsor_persona.public_hex,
    ) == {
        "status": "pending",
        "genesis_id": founded.genesis_id,
        "granted_role": "sponsor-only",
        "have": 0,
        "need": 1,
        "approvals": [],
        "admitting": [],
        "position": {
            "parents": list(sponsor_claim.parents),
            "hlc": sponsor_claim.hlc.to_list(),
        },
    }
    founder_entry = sign_approval(founder, "member.claim", claim_payload)
    assert claim_service.countersign(
        slug,
        invite_event.event_id,
        sponsor_persona.public_hex,
        founder_entry,
    ) == {
        "status": "ready",
        "have": 1,
        "need": 1,
        "kem_credential": None,
        "admitting": [founder.public_hex],
        "position": {
            "parents": list(sponsor_claim.parents),
            "hlc": sponsor_claim.hlc.to_list(),
        },
    }

    # Staging TTL and pre-migration rows are terminal service outcomes, not
    # retryable pending states and not countersignature merge targets.
    with LedgerStore(path) as store:
        sponsor_key = store.claim_key(
            invite_event.event_id,
            sponsor_persona.public_hex,
        )
        legacy_key = store.claim_key(
            self_invite_ref,
            result["tokenSelfClaim"]["personaPub"],
        )
        with store.db:
            store.db.execute(
                "UPDATE ledger_pending_claims SET staged_at = 0 "
                "WHERE claim_key = ?",
                (sponsor_key,),
            )
            store.db.execute(
                "UPDATE ledger_pending_claims SET parents = NULL "
                "WHERE claim_key = ?",
                (legacy_key,),
            )
    assert claim_service.status(
        slug,
        invite_event.event_id,
        sponsor_persona.public_hex,
    ) == {"status": "rejected", "reason": "claim-expired"}
    sponsor_final, _ = mint_member_claim(
        sponsor_seed,
        founded.genesis_id,
        invite_ref=invite_event.event_id,
        heads=sponsor_claim.parents,
        hlc=sponsor_claim.hlc,
        token=sponsor_token,
        approvals=[founder_entry],
    )
    assert claim_service.submit(slug, sponsor_final.to_json()) == {
        "status": "rejected",
        "reason": "claim-expired",
    }
    assert claim_service.countersign(
        slug,
        invite_event.event_id,
        sponsor_persona.public_hex,
        founder_entry,
    ) == {"status": "rejected", "reason": "claim-expired"}
    assert claim_service.status(
        slug,
        self_invite_ref,
        result["tokenSelfClaim"]["personaPub"],
    ) == {"status": "rejected", "reason": "legacy-staging"}

    # cz4fb: a claim staged before invite expiry can finalize after both the
    # wall clock and current DAG frontier pass expiry, but only at its exact
    # server-stored position. A current-frontier event keeps both gates.
    delayed_token = "f6" * 32
    delayed_seed = bytes(range(1, 33))
    delayed_expiry = wall_now + 60_000
    with LedgerStore(path) as store:
        max_hlc = max(store.get(head).hlc for head in store.heads())
        delayed_invite = make_event(
            founder,
            {
                "type": "invite",
                "granted_role": "self-member",
                "expiry": delayed_expiry,
                "sponsor": founder.public_hex,
                "token_hash": hashlib.sha256(
                    delayed_token.encode("utf-8")
                ).hexdigest(),
            },
            store.heads(),
            max_hlc.tick(max_hlc.ts),
        )
        store.append(delayed_invite)
        delayed_claim, _ = mint_member_claim(
            delayed_seed,
            founded.genesis_id,
            invite_ref=delayed_invite.event_id,
            heads=store.heads(),
            hlc=delayed_invite.hlc.tick(delayed_invite.hlc.ts),
            token=delayed_token,
        )
    assert claim_service.submit(slug, delayed_claim.to_json()) == {
        "status": "pending",
        "have": 0,
        "need": 1,
    }
    delayed_persona = derive_persona(delayed_seed, founded.genesis_id)
    with LedgerStore(path) as store:
        delayed_key = store.claim_key(
            delayed_invite.event_id,
            delayed_persona.public_hex,
        )
        staged_at = store.get_pending_claim(delayed_key)["staged_at"]
    assert claim_service.submit(slug, delayed_claim.to_json()) == {
        "status": "pending",
        "have": 0,
        "need": 1,
    }
    with LedgerStore(path) as store:
        assert store.get_pending_claim(delayed_key)["staged_at"] == staged_at

    delayed_approval = sign_approval(
        founder,
        "member.claim",
        delayed_claim.payload,
    )
    delayed_ready = claim_service.countersign(
        slug,
        delayed_invite.event_id,
        delayed_persona.public_hex,
        delayed_approval,
    )
    assert delayed_ready["status"] == "ready"
    assert delayed_ready["position"] == {
        "parents": list(delayed_claim.parents),
        "hlc": delayed_claim.hlc.to_list(),
    }

    with LedgerStore(path) as store:
        filler = KeyPair.generate()
        store.append(make_event(
            root,
            {
                "type": "delegate",
                "child_pub": filler.public_hex,
                "scope": ["link:publish"],
                "can_redelegate": False,
                "proof": sign_delegate_proof(
                    filler, founded.genesis_id,
                    root.public_hex, ["link:publish"],
                ),
            },
            store.heads(),
            HLC(delayed_expiry + 1_000),
        ))
        current_frontier, _ = mint_member_claim(
            delayed_seed,
            founded.genesis_id,
            invite_ref=delayed_invite.event_id,
            heads=store.heads(),
            hlc=HLC(delayed_expiry + 2_000),
            token=delayed_token,
            approvals=[delayed_approval],
        )
    position = delayed_ready["position"]
    pinned, _ = mint_member_claim(
        delayed_seed,
        founded.genesis_id,
        invite_ref=delayed_invite.event_id,
        heads=position["parents"],
        hlc=HLC.from_value(position["hlc"]),
        token=delayed_token,
        approvals=[delayed_approval],
    )
    monkeypatch.setattr(
        claim_service.time,
        "time",
        lambda: (delayed_expiry + 1_000) / 1_000,
    )
    assert claim_service.submit(slug, current_frontier.to_json()) == {
        "status": "rejected",
        "reason": "invite-expired",
    }
    assert claim_service.submit(slug, pinned.to_json())["status"] == "admitted"
    with LedgerStore(path) as store:
        assert delayed_persona.public_hex in store.fold().members
