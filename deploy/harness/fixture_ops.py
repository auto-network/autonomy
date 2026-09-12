"""Narrow fixture setup/inspection operations for the Docker ladder.

These operations run *inside* the real relay or node image.  They establish
throwaway identities and the registry binding that D21 does not yet expose in
production.  They do not implement a second transport: joining and content
fetching still use the production ViewerChannel, relay, connector, claim
service, and ledger fold.

JSON payloads arrive on stdin so neither invitation bearer nor private keys
appear in a container command line.  Results never echo those secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ISO = "%Y-%m-%dT%H:%M:%SZ"


class FixtureError(RuntimeError):
    """The throwaway harness fixture could not be established."""


def _input() -> dict:
    try:
        value = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        raise FixtureError(f"stdin must carry one JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise FixtureError("stdin must carry one JSON object")
    return value


def _output(value: dict) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _setting_payload(set_id: str, org: str | None, key: str = "default") -> dict:
    from tools.graph import settings_ops

    members = settings_ops.read_owned_set(set_id, org=org).members
    member = next((row for row in members if row.key == key), None)
    if member is None or not isinstance(member.payload, dict):
        raise FixtureError(f"missing {set_id}#{key}")
    return member.payload


def _personal_seed(password: str) -> bytes:
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit.root_factor_policy import open_armor_with_password

    row = _setting_payload(PERSONAL_IDENTITY_SET_ID, None)
    return bytes.fromhex(
        open_armor_with_password(row["armored_private_key"], password).private_hex
    )


def _org_root(org: str, personal_seed: bytes):
    from tools.graph.schemas.network_identity import (
        NETWORK_ORG_KEY_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    from tools.network.idkit import KeyPair
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    from tools.network.idkit.sealing import open as open_sealed

    row = _setting_payload(NETWORK_ORG_KEY_SET_ID, org)
    recipient_private, _ = derive_encapsulation_keypair(
        personal_seed, ORG_ROOT_ARMOR_PURPOSE
    )
    seed = open_sealed(
        bytes.fromhex(row["sealed_root_key"]),
        recipient_private,
        row["seal_purpose"],
    )
    return KeyPair.from_private_hex(seed.hex())


def _put_local_grant(
    *,
    org: str,
    token: str,
    target_uuid: str,
    target_type: str,
    subject_id: str,
    invite_ref: str | None = None,
) -> str:
    from tools.dashboard.link_channel_key import mint_channel_key
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
    )

    channel_pub = mint_channel_key(token, org)
    payload = {
        "token": token,
        "url": f"https://relay.harness.invalid/l/{token}",
        "target_uuid": target_uuid,
        "target_type": target_type,
        "meta": {},
        "subject": {"kind": "operator", "id": subject_id},
        "issued_at": time.strftime(ISO, time.gmtime()),
        "channel_pub": channel_pub,
    }
    if invite_ref is not None:
        payload["invite_ref"] = invite_ref
    settings_ops.upsert_by_key(
        NETWORK_LINK_GRANT_SET_ID,
        NETWORK_LINK_GRANT_REVISION,
        token,
        payload,
        org=org,
    )
    return channel_pub


def _write_mode_0600(path: Path, value: str) -> None:
    """Atomically materialize a fixture service key without a 0644 window."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def provision_fleet_runtime(*, personal_root, org_uuid: str) -> None:
    """Enroll one synthetic machine and arm its organization connector."""
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_identity import (
        MACHINE_IDENTITY_KEY,
        MACHINE_IDENTITY_REVISION,
        MACHINE_IDENTITY_SET_ID,
    )
    from tools.graph.schemas.network_identity import (
        NETWORK_BINDING_REVISION,
        NETWORK_BINDING_SET_ID,
    )
    from tools.network import fleet_roster
    from tools.network.fleet_relay_sync import FleetRuntimeWarmCache
    from tools.network.idkit import KeyPair, Subject, issue_cert

    machine = KeyPair.generate()
    process = KeyPair.generate()
    machine_id = machine.public_hex
    now = int(time.time())
    fleet_roster.store_entry(
        fleet_roster.enroll(
            personal_root,
            machine_id=machine_id,
            machine_pub=machine.public_hex,
        ),
        org=None,
    )
    settings_ops.upsert_by_key(
        MACHINE_IDENTITY_SET_ID,
        MACHINE_IDENTITY_REVISION,
        MACHINE_IDENTITY_KEY,
        {"machine_id": machine_id},
        org="machine",
        state="raw",
    )
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "fixture-runtime",
        {
            "org_uuid": org_uuid,
            "root_pub": personal_root.public_hex,
            "registry_url": "http://relay:8477",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": time.strftime(
                ISO, time.gmtime(now + 24 * 60 * 60)
            ),
        },
        org="personal",
        state="raw",
    )
    delegation = issue_cert(
        machine,
        process.public_hex,
        scope=("fleet:sync",),
        org=f"personal:{personal_root.public_hex}",
        subject=Subject("machine", machine_id),
        not_before=now - 30,
        not_after=now + 3600,
    )
    reachability = issue_cert(
        personal_root,
        machine.public_hex,
        scope=("node:announce", "node:lookup"),
        org=org_uuid,
        subject=Subject("machine", machine_id),
        not_before=now - 30,
        not_after=now + 3600,
    )
    FleetRuntimeWarmCache(org_uuid).store({
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": delegation.to_dict(),
        "machine_private_seed": machine.private_hex,
        "reachability_cert": reachability.to_dict(),
    })


def found_node(payload: dict) -> dict:
    """Found A and establish the local side of its two real relay grants."""
    from tools.data_paths import resolve_store
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB
    from tools.graph.models import Source, Thought
    from tools.graph.schemas.network_identity import (
        NETWORK_BINDING_REVISION,
        NETWORK_BINDING_SET_ID,
        NETWORK_ORG_KEY_REVISION_2,
        NETWORK_ORG_KEY_SET_ID,
        NETWORK_SERVE_CERT_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit import KeyPair, Subject, derive_persona, issue_cert
    from tools.network.idkit.root_factor_policy import mint_password_armor
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
    from tools.network.ledger import HLC, LedgerStore, make_event, org_ledger_db_path
    from tools.network.ledger.found import found_org_ledger

    required = {
        "org", "password", "claim_token", "join_channel_token",
        "content_channel_token",
    }
    if set(payload) != required:
        raise FixtureError(f"found payload must carry exactly {sorted(required)}")
    org = payload["org"]
    password = payload["password"]
    if not all(isinstance(payload[key], str) and payload[key] for key in required):
        raise FixtureError("found payload fields must be non-empty strings")

    db = GraphDB.open_org_db(org)
    try:
        row = db.conn.execute("SELECT id FROM orgs WHERE slug = ?", (org,)).fetchone()
        if row is None:
            raise FixtureError("first-run org has no identity row")
        org_uuid = row["id"]
    finally:
        db.close()

    personal = KeyPair.generate()
    org_root = KeyPair.generate()
    personal_seed = bytes.fromhex(personal.private_hex)
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID,
            1,
            "default",
            {
                "armored_private_key": mint_password_armor(personal, password),
                "root_pub": personal.public_hex,
                "display_name": "Harness operator",
                "created_at": time.strftime(ISO, time.gmtime()),
            },
            org=None,
        )
    recipient_private, recipient_public = derive_encapsulation_keypair(
        personal_seed, ORG_ROOT_ARMOR_PURPOSE
    )
    del recipient_private
    settings_ops.upsert_by_key(
        NETWORK_ORG_KEY_SET_ID,
        NETWORK_ORG_KEY_REVISION_2,
        "default",
        {
            "root_pub": org_root.public_hex,
            "sealed_root_key": seal(
                bytes.fromhex(org_root.private_hex),
                recipient_public,
                ORG_ROOT_ARMOR_PURPOSE,
            ).hex(),
            "owner_kem_pub": recipient_public,
            "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        },
        org=org,
    )

    now_ms = int(time.time() * 1000)
    with LedgerStore(org_ledger_db_path(org)) as store:
        founded = found_org_ledger(
            store,
            org_id=org_uuid,
            org_root=org_root,
            personal_root_seed=personal_seed,
            now=now_ms,
        )
        role_id = store.append(
            make_event(
                org_root,
                {
                    "type": "role.define",
                    "name": "member",
                    "scope_set": ["graph:read"],
                    "claim_requires": "admin-ack",
                    "approver_threshold": {"kind": "static", "count": 2},
                    "version": 1,
                },
                sorted(store.heads()),
                HLC(now_ms, 4),
            )
        )
        founder = derive_persona(personal_seed, founded.genesis_id)
        invite_expiry = now_ms + 30 * 60 * 1000
        invite_ref = store.append(
            make_event(
                founder,
                {
                    "type": "invite",
                    "granted_role": "member",
                    "expiry": invite_expiry,
                    "sponsor": founder.public_hex,
                    "token_hash": hashlib.sha256(
                        payload["claim_token"].encode("utf-8")
                    ).hexdigest(),
                },
                [role_id],
                HLC(now_ms, 5),
            )
        )

    # Establish the same audited storage authority an interactive sign-in
    # establishes before publishing links.  The channel seed is an
    # organization-vaulted Setting, so the fixture must not mint it until a
    # current member persona has delegated the two bounded storage scopes and
    # the Personal vault can retain that delegate across process restarts.
    from tools.dashboard import org_storage_delegate
    from tools.network.storagekit.delegate import provision as provision_delegate
    from tools.vault.bringup import register_vault_for_unlock
    from tools.vault.key_holder import _scoped_db
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.vault.store import VaultStore
    from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID

    audited_private, audited_public = derive_delegate_audited_recipient(personal_seed)
    with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, None)) as vault:
        vault.put_delegate_audited_recipient(audited_public)
    settings_ops.set_personal_delegate_audited_key(audited_private)

    with LedgerStore(org_ledger_db_path(org)) as store:
        delegate = provision_delegate(
            store.ledger,
            founder,
            founder,
            founded.genesis_id,
            hlc=HLC(now_ms, 6),
            ttl_ms=org_storage_delegate.TTL_MS,
        )
        delegate_event = store.ledger.get(delegate.grant_event_id)
    org_storage_delegate.accept({
        "organization": org,
        "action": "new",
        "private_key": delegate.signing_key.private_hex,
        "event": delegate_event.to_json().decode("utf-8"),
    })

    from tools.network.ledger import membership_commitment as membership
    with LedgerStore(org_ledger_db_path(org)) as store:
        state = store.fold()
        heads = sorted(store.heads())
    if len(heads) != 1:
        raise FixtureError("founded organization must have one checkpoint head")
    membership_checkpoint = membership.build_root_checkpoint(
        org=org_uuid,
        seq=0,
        genesis_id=founded.genesis_id,
        ledger_head=heads[0],
        members_root_hex=membership.members_root(state),
        checkpointers_root_hex=membership.checkpointers_root(state),
        ts=int(time.time()),
        root=org_root,
    )

    def fixture_ledger_provider(_set_id, selected_org):
        if selected_org != org:
            return None
        ledger = LedgerStore(org_ledger_db_path(selected_org))
        return (
            ledger.fold(),
            lambda heads: ledger.fold(heads=list(heads)),
            ledger.ledger.ancestry,
        )

    register_vault_for_unlock(
        generation_keys={},
        author_provider=org_storage_delegate.signing_key,
        org_ledger_provider=lambda selected_org: fixture_ledger_provider(
            None, selected_org
        ),
    )

    content_id = str(uuid.uuid4())
    # Write the served note through the SAME resolution reads use:
    # GraphDB(org=...) -> resolve_caller_db_path, which honors a pinned
    # GRAPH_DB. open_org_db() bypasses the pin and lands the note in
    # data/orgs/<slug>.db, where the pinned-GRAPH_DB serving + dashboard reads
    # never look -> the note serves "unavailable" / "not found in caller
    # scope". Keeping the note on the same DB as the grants (both honor the
    # pin) is what makes resolve_target/op:fetch and /graph/<id> resolve it.
    db = GraphDB(org=org)
    try:
        db.insert_source(
            Source(
                id=content_id,
                type="note",
                title="Portable node proof",
                publication_state="canonical",
            )
        )
        db.insert_thought(
            Thought(
                source_id=content_id,
                content="This note crossed the real relay from restored node C.",
                role="user",
                turn_number=1,
            )
        )
        db.conn.commit()
    finally:
        db.close()

    expires_iso = time.strftime(
        ISO, time.gmtime(time.time() + 24 * 60 * 60)
    )
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "relay",
        {
            "org_uuid": org_uuid,
            "root_pub": org_root.public_hex,
            "registry_url": "http://relay:8477",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": expires_iso,
        },
        org=org,
    )
    provision_fleet_runtime(personal_root=personal, org_uuid=org_uuid)
    join_channel_pub = _put_local_grant(
        org=org,
        token=payload["join_channel_token"],
        target_uuid=org_uuid,
        target_type="org:join",
        invite_ref=invite_ref,
        subject_id=founder.public_hex,
    )
    content_channel_pub = _put_local_grant(
        org=org,
        token=payload["content_channel_token"],
        target_uuid=content_id,
        target_type="note",
        subject_id=founder.public_hex,
    )

    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        founder,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=org_uuid,
        subject=Subject("persona", founder.public_hex),
        not_before=now - 30,
        not_after=now + 24 * 60 * 60,
    )
    key_name = f"serve-{org_uuid}-{delegate.public_hex}.key"
    key_dir = resolve_store("serving_keys")
    key_path = key_dir / key_name
    _write_mode_0600(key_path, delegate.private_hex)
    settings_ops.upsert_by_key(
        NETWORK_SERVE_CERT_SET_ID,
        3,
        "default",
        {
            "cert": cert.to_json().decode("ascii"),
            "key_path": key_name,
            "persona_pub": founder.public_hex,
            "not_after": cert.not_after,
        },
        org=org,
    )
    return {
        "org": org,
        "org_uuid": org_uuid,
        "root_pub": org_root.public_hex,
        "personal_root_pub": personal.public_hex,
        "genesis_id": founded.genesis_id,
        "founder_persona_pub": founder.public_hex,
        "invite_ref": invite_ref,
        "invite_expiry": invite_expiry,
        "content_id": content_id,
        "join_channel_pub": join_channel_pub,
        "content_channel_pub": content_channel_pub,
        "serve_delegate_pub": delegate.public_hex,
        "serve_delegate_sha256": hashlib.sha256(
            delegate.private_hex.encode("ascii")
        ).hexdigest(),
        "membership_checkpoint": membership_checkpoint,
    }


def seed_registry(payload: dict) -> dict:
    """Fixture the D21 org binding and two opaque relay grants."""
    from tools.network.registry.store import LinkGrant, RegistryStore

    required = {
        "org_uuid", "root_pub", "join_channel_token", "invite_ref",
        "invite_expiry", "content_channel_token", "content_id",
        "membership_checkpoint",
    }
    if set(payload) != required:
        raise FixtureError(
            f"seed-registry payload must carry exactly {sorted(required)}"
        )
    now = int(time.time())
    store = RegistryStore(
        os.environ.get("AUTONOMY_HARNESS_REGISTRY_DB", "/registry/registry.db")
    )
    try:
        if store.get_org(payload["org_uuid"]) is None:
            store.create_org(
                payload["org_uuid"],
                payload["root_pub"],
                json.dumps({"mode": "none"}, sort_keys=True),
                None,
                now=now,
                expires_at=now + 24 * 60 * 60,
            )
        store.advance_membership_state(
            payload["org_uuid"], payload["membership_checkpoint"], now=now
        )
        store.create_link(
            LinkGrant(
                token=payload["join_channel_token"],
                org_uuid=payload["org_uuid"],
                target_uuid=payload["org_uuid"],
                target_type="org:join",
                invite_ref=payload["invite_ref"],
                meta={},
                created_at=now,
                expires_at=None,
                expires_at_ms=payload["invite_expiry"],
                revoked_at=None,
                signer_pub=payload["root_pub"],
                subject_kind="operator",
                subject_id="harness-fixture",
            )
        )
        store.create_link(
            LinkGrant(
                token=payload["content_channel_token"],
                org_uuid=payload["org_uuid"],
                target_uuid=payload["content_id"],
                target_type="note",
                meta={},
                created_at=now,
                expires_at=now + 24 * 60 * 60,
                revoked_at=None,
                signer_pub=payload["root_pub"],
                subject_kind="operator",
                subject_id="harness-fixture",
            )
        )
    finally:
        store.close()
    return {
        "seeded": True,
        "org_uuid": payload["org_uuid"],
        "join_url": (
            "https://relay.harness.invalid/l/"
            + payload["join_channel_token"]
        ),
    }


def activate_serving(payload: dict) -> dict:
    """Retry the live serve-cert operation so its dashboard owns reconcile."""
    if set(payload) != {"org", "password"} \
            or not all(isinstance(payload[key], str) and payload[key] for key in payload):
        raise FixtureError("activate-serving requires exactly org and password")
    from tools.data_paths import resolve_store
    from tools.dashboard.link_serving_supervisor import local_serve_cert_member
    from tools.dashboard.unlock_routes import UNLOCK_SIGNING_DOMAIN
    from tools.network.idkit import KeyPair, canonical_json

    member = local_serve_cert_member(payload["org"])
    if member is None:
        raise FixtureError("serving credential is absent")
    stored = member.payload
    key_path = resolve_store("serving_keys") / stored["key_path"]
    body = {
        "org": payload["org"],
        "cert": stored["cert"],
        "persona_pub": stored["persona_pub"],
        "private_key": key_path.read_text(encoding="ascii").strip(),
    }
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    options_request = urllib.request.Request(
        "http://127.0.0.1:8080/api/identity/unlock/password/options",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(options_request, timeout=20) as response:
        options = json.loads(response.read())
    personal = KeyPair.from_private_hex(_personal_seed(payload["password"]).hex())
    signed = UNLOCK_SIGNING_DOMAIN + canonical_json({
        "v": 1,
        "challenge": options["challenge"],
        "origin": options["origin"],
    })
    unlock_request = urllib.request.Request(
        "http://127.0.0.1:8080/api/identity/unlock/password",
        data=json.dumps({
            "challenge": options["challenge"],
            "signature": personal.sign_hex(signed),
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(unlock_request, timeout=20) as response:
        unlocked = json.loads(response.read())
    if unlocked.get("ok") is not True:
        raise FixtureError(f"dashboard unlock refused: {unlocked!r}")

    request = urllib.request.Request(
        "http://127.0.0.1:8080/api/network/serve-cert",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Graph-Org": payload["org"],
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise FixtureError(
            f"serve-cert retry failed ({exc.code}): {exc.read().decode('utf-8', 'replace')}"
        ) from exc
    if result.get("ok") is not True:
        raise FixtureError(f"serve-cert retry refused: {result!r}")
    return {"ok": True, "child_pub": result.get("child_pub")}


def pending_join(_payload: dict) -> dict:
    from tools.dashboard.dao import pending_joins

    rows = pending_joins.list_pending()
    if len(rows) != 1:
        return {"state": "not-single", "count": len(rows)}
    row = rows[0]
    return {
        "state": "pending",
        "org": row["org"],
        "invite_ref": row["invite_ref"],
        "persona_pub": row["persona_pub"],
        "claim_key": row["claim_key"],
        "have": row["have"],
        "need": row["need"],
    }


def approvals(payload: dict) -> dict:
    from tools.network.idkit import derive_persona
    from tools.network.ledger import LedgerStore, org_ledger_db_path, sign_approval

    if set(payload) != {"org", "password", "claim_key"}:
        raise FixtureError("approvals payload must carry org, password, claim_key")
    personal_seed = _personal_seed(payload["password"])
    org_root = _org_root(payload["org"], personal_seed)
    with LedgerStore(org_ledger_db_path(payload["org"])) as store:
        pending = store.get_pending_claim(payload["claim_key"])
        if pending is None:
            raise FixtureError("pending claim is absent")
        genesis_id = store.ledger.genesis.event_id
    founder = derive_persona(personal_seed, genesis_id)
    entries = [
        sign_approval(org_root, "member.claim", pending["body"]),
        sign_approval(founder, "member.claim", pending["body"]),
    ]
    return {"approvals": entries}


def inspect_node(payload: dict) -> dict:
    from tools.graph.schemas.network_identity import (
        NETWORK_SERVE_CERT_SET_ID,
    )
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.ledger import LedgerStore, org_ledger_db_path

    if set(payload) != {"org", "password", "persona_pub"}:
        raise FixtureError("inspect payload must carry org, password, persona_pub")
    seed = _personal_seed(payload["password"])
    root = _org_root(payload["org"], seed)
    with LedgerStore(org_ledger_db_path(payload["org"])) as store:
        state = store.fold()
        genesis_id = store.ledger.genesis.event_id
        member = state.members.get(payload["persona_pub"])
    cert = _setting_payload(NETWORK_SERVE_CERT_SET_ID, payload["org"])
    key_path = Path(cert["key_path"])
    if not key_path.is_absolute():
        from tools.data_paths import resolve_store

        key_path = resolve_store("serving_keys") / key_path
    return {
        "org_root_pub": root.public_hex,
        "personal_root_pub": _setting_payload(
            PERSONAL_IDENTITY_SET_ID, None
        )["root_pub"],
        "genesis_id": genesis_id,
        "member_present": member is not None,
        "member_roles": sorted(member.roles) if member is not None else [],
        "serve_delegate_sha256": hashlib.sha256(
            key_path.read_text(encoding="ascii").strip().encode("ascii")
        ).hexdigest(),
        "serve_key_mode": stat.S_IMODE(key_path.stat().st_mode),
    }


def relay_stats(payload: dict) -> dict:
    """Read-only relay-store state for the transcript (auto-qqlz5).

    Reports ``link_sessions`` counts under their TRUE name — browser
    identity-linking sessions (E1 state written only by /v1/link/challenge
    and assertion redemption; relay-network ruling 2026-08-13) — plus live
    ``node_hints``. Neither is a serving proxy: a headless ladder correctly
    shows zero for both, and the serving proof is the SERVED join context
    the driver prints separately. Opens the registry database read-only so
    evidence capture cannot mutate the store; no relay code is touched.
    """
    import sqlite3

    org_uuid = payload.get("org_uuid")
    if not isinstance(org_uuid, str) or not org_uuid:
        raise FixtureError("relay-stats payload must carry org_uuid")
    db_path = os.environ.get(
        "AUTONOMY_HARNESS_REGISTRY_DB", "/registry/registry.db"
    )
    now = int(time.time())
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sessions_live = conn.execute(
            "SELECT COUNT(*) FROM link_sessions"
            " WHERE (org_uuid = ? OR org_uuid IS NULL) AND expires_at >= ?",
            (org_uuid, now),
        ).fetchone()[0]
        sessions_ever = conn.execute(
            "SELECT COUNT(*) FROM link_sessions"
            " WHERE org_uuid = ? OR org_uuid IS NULL",
            (org_uuid,),
        ).fetchone()[0]
        hints_live = conn.execute(
            "SELECT COUNT(*) FROM node_hints"
            " WHERE org_uuid = ? AND expires_at >= ?",
            (org_uuid, now),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "org_uuid": org_uuid,
        "browser_sessions_live": sessions_live,
        "browser_sessions_ever": sessions_ever,
        "node_hints_live": hints_live,
    }


COMMANDS = {
    "activate-serving": activate_serving,
    "found": found_node,
    "seed-registry": seed_registry,
    "pending": pending_join,
    "approvals": approvals,
    "inspect": inspect_node,
    "relay-stats": relay_stats,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    try:
        _output(COMMANDS[args.command](_input()))
    except FixtureError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
