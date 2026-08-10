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
import json
import os
import stat
import sys
import time
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
    from tools.network.idkit.armor import decrypt_root_key

    row = _setting_payload(PERSONAL_IDENTITY_SET_ID, None)
    return bytes.fromhex(
        decrypt_root_key(row["armored_private_key"], password).private_hex
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
) -> None:
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
    )

    payload = {
        "token": token,
        "url": f"https://relay.harness.invalid/l/{token}",
        "target_uuid": target_uuid,
        "target_type": target_type,
        "meta": {},
        "subject": {"kind": "operator", "id": subject_id},
        "issued_at": time.strftime(ISO, time.gmtime()),
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
        NETWORK_SERVE_CERT_REVISION,
        NETWORK_SERVE_CERT_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit import KeyPair, Subject, derive_persona, issue_cert
    from tools.network.idkit.armor import encrypt_root_key
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
                "armored_private_key": encrypt_root_key(personal, password),
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
    _put_local_grant(
        org=org,
        token=payload["join_channel_token"],
        target_uuid=org_uuid,
        target_type="org:join",
        invite_ref=invite_ref,
        subject_id=founder.public_hex,
    )
    _put_local_grant(
        org=org,
        token=payload["content_channel_token"],
        target_uuid=content_id,
        target_type="note",
        subject_id=founder.public_hex,
    )

    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        org_root,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=org_uuid,
        subject=Subject("operator", "harness-serving"),
        not_before=now - 30,
        not_after=now + 24 * 60 * 60,
    )
    key_name = f"serve-{org_uuid}.key"
    key_dir = resolve_store("serving_keys")
    key_path = key_dir / key_name
    _write_mode_0600(key_path, delegate.private_hex)
    settings_ops.upsert_by_key(
        NETWORK_SERVE_CERT_SET_ID,
        NETWORK_SERVE_CERT_REVISION,
        "default",
        {
            "cert": cert.to_json().decode("ascii"),
            "key_path": key_name,
            "root_pub": org_root.public_hex,
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
        "serve_delegate_pub": delegate.public_hex,
        "serve_delegate_sha256": hashlib.sha256(
            delegate.private_hex.encode("ascii")
        ).hexdigest(),
    }


def seed_registry(payload: dict) -> dict:
    """Fixture the D21 org binding and two opaque relay grants."""
    from tools.network.registry.store import LinkGrant, RegistryStore

    required = {
        "org_uuid", "root_pub", "join_channel_token", "invite_ref",
        "invite_expiry", "content_channel_token", "content_id",
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


COMMANDS = {
    "found": found_node,
    "seed-registry": seed_registry,
    "pending": pending_join,
    "approvals": approvals,
    "inspect": inspect_node,
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
