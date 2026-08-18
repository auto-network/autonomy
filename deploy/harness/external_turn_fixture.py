"""Create and retire one isolated org used by production TURN acceptance.

This module runs in a subprocess with its own ``AUTONOMY_DATA_ROOT``.  Secret
material is accepted on stdin and returned only to the parent process's
captured stdout; it never appears in argv or the durable evidence report.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ISO = "%Y-%m-%dT%H:%M:%SZ"


class FixtureError(RuntimeError):
    pass


def _input() -> dict:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise FixtureError("stdin must carry one JSON object")
    return value


def _request(registry: str, method: str, path: str, key, payload: dict) -> dict:
    from tools.network.registry.signing import sign_request

    envelope = sign_request(key, method, path, payload, ts=int(time.time()))
    request = urllib.request.Request(
        registry + path,
        data=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(
            request, timeout=20, context=ssl.create_default_context()
        ) as response:
            return {"status": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "body": exc.read().decode("utf-8", "replace"),
        }


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(value)


def setup(payload: dict) -> dict:
    required = {
        "slug", "password", "registry_url", "link_base_url", "ttl",
        "site_bytes",
    }
    if set(payload) != required:
        raise FixtureError(f"setup fields must be exactly {sorted(required)}")
    slug = payload["slug"]
    password = payload["password"]
    registry = payload["registry_url"].rstrip("/")
    link_base = payload["link_base_url"].rstrip("/")
    ttl = payload["ttl"]
    site_bytes = payload["site_bytes"]
    if (
        not isinstance(slug, str)
        or not slug
        or not isinstance(password, str)
        or not password
        or type(ttl) is not int
        or ttl < 3600
        or type(site_bytes) is not int
        or site_bytes <= 60 * 1024
    ):
        raise FixtureError("setup values are malformed")

    from tools.dashboard.dao import mission_control_db as mcdb
    from tools.data_paths import resolve_store
    from tools.graph import org_ops, settings_ops
    from tools.graph.db import GraphDB
    from tools.graph.schemas.network_identity import (
        NETWORK_BINDING_REVISION,
        NETWORK_BINDING_SET_ID,
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
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
    from tools.network.ledger import LedgerStore, org_ledger_db_path
    from tools.network.ledger.found import found_org_ledger

    # Mission composition reads platform state from the autonomy scope.  Both
    # stores belong to this isolated root; neither falls back to host data.
    org_ops.ensure_bootstrap_orgs(first_org="autonomy")
    org_ops.ensure_bootstrap_orgs(
        first_org=slug, first_org_name=f"TURN acceptance {slug}"
    )
    db = GraphDB.open_org_db(slug)
    try:
        row = db.conn.execute(
            "SELECT id FROM orgs WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise FixtureError("isolated org bootstrap did not create an identity")
        org_uuid = row["id"]
    finally:
        db.close()

    personal = KeyPair.generate()
    org_root = KeyPair.generate()
    delegate = KeyPair.generate()
    personal_seed = bytes.fromhex(personal.private_hex)
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID,
            1,
            "default",
            {
                "armored_private_key": encrypt_root_key(personal, password),
                "root_pub": personal.public_hex,
                "display_name": "TURN acceptance",
                "created_at": time.strftime(ISO, time.gmtime()),
            },
            org=None,
        )
    _, recipient_public = derive_encapsulation_keypair(
        personal_seed, ORG_ROOT_ARMOR_PURPOSE
    )
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
        org=slug,
    )
    now_ms = int(time.time() * 1000)
    with LedgerStore(org_ledger_db_path(slug)) as store:
        founded = found_org_ledger(
            store,
            org_id=org_uuid,
            org_root=org_root,
            personal_root_seed=personal_seed,
            now=now_ms,
        )
        founder = derive_persona(personal_seed, founded.genesis_id)

    mission_db = Path(os.environ["MISSION_CONTROL_DB"])
    mcdb.DB_PATH = mission_db
    mcdb.init_db(mission_db)
    mission = mcdb.create_mission(
        "Production TURN acceptance", "turn-acceptance", db_path=mission_db
    )
    mission_id = mission["mission_id"]
    prefix = "<html><body><h1>Production TURN acceptance</h1><pre>"
    suffix = "</pre></body></html>"
    site = prefix + ("m" * max(1, site_bytes - len(prefix) - len(suffix))) + suffix
    mcdb.push_site_revision(mission_id, site, db_path=mission_db)
    pillar = mcdb.create_pillar(
        mission_id,
        "Transport evidence",
        "turn-acceptance",
        "#34d399",
        db_path=mission_db,
    )
    mcdb.push_pillar_site_revision(
        pillar["pillar_id"],
        "<html><body>TURN acceptance detail</body></html>",
        db_path=mission_db,
    )
    guest = mcdb.create_visitor_token("TURN acceptance guest", db_path=mission_db)

    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "relay",
        {
            "org_uuid": org_uuid,
            "root_pub": org_root.public_hex,
            "registry_url": registry,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": time.strftime(
                ISO, time.gmtime(time.time() + ttl)
            ),
        },
        org=slug,
    )

    now = int(time.time())
    cert = issue_cert(
        org_root,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=org_uuid,
        subject=Subject("persona", founder.public_hex),
        not_before=now - 30,
        not_after=now + ttl,
    )
    viewer_cert = issue_cert(
        org_root,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=org_uuid,
        subject=Subject("operator", delegate.public_hex),
        not_before=cert.not_before,
        not_after=cert.not_after,
    )
    key_dir = resolve_store("serving_keys")
    key_dir.mkdir(parents=True, exist_ok=True)
    base = key_dir / f"serve-{org_uuid}-{delegate.public_hex}"
    key_path = Path(str(base) + ".key")
    cert_path = Path(str(base) + ".cert")
    viewer_cert_path = Path(str(base) + ".viewer.cert")
    control_path = Path(str(base) + ".ctl")
    _write_secret(key_path, delegate.private_hex)
    _write_secret(cert_path, cert.to_json().decode("ascii"))
    _write_secret(viewer_cert_path, viewer_cert.to_json().decode("ascii"))
    settings_ops.upsert_by_key(
        NETWORK_SERVE_CERT_SET_ID,
        NETWORK_SERVE_CERT_REVISION,
        "default",
        {
            "cert": cert.to_json().decode("ascii"),
            "viewer_cert": viewer_cert.to_json().decode("ascii"),
            "key_path": key_path.name,
            "root_pub": org_root.public_hex,
            "not_after": cert.not_after,
        },
        org=slug,
    )

    registered = _request(
        registry,
        "POST",
        "/v1/orgs",
        org_root,
        {
            "org_uuid": org_uuid,
            "root_pub": org_root.public_hex,
            "recovery_policy": "none",
            "requested_ttl": ttl,
        },
    )
    if registered["status"] != 201:
        raise FixtureError(f"production org registration refused: {registered['status']}")
    published = _request(
        registry,
        "POST",
        "/v1/links",
        org_root,
        {
            "org": org_uuid,
            "target_uuid": mission_id,
            "target_type": "mission",
            "meta": {"ttl": ttl, "label": "TURN acceptance"},
        },
    )
    if published["status"] != 201 or not isinstance(published["body"], dict):
        raise FixtureError(f"production link publish refused: {published['status']}")
    token = published["body"]["token"]
    settings_ops.upsert_by_key(
        NETWORK_LINK_GRANT_SET_ID,
        NETWORK_LINK_GRANT_REVISION,
        token,
        {
            "token": token,
            "url": f"{link_base}/l/{token}",
            "target_uuid": mission_id,
            "target_type": "mission",
            "meta": {
                "ice_policy": "relay_only",
                "participant_id": guest["participant_id"],
            },
            "subject": {"kind": "operator", "id": founder.public_hex},
            "issued_at": time.strftime(ISO, time.gmtime()),
        },
        org=slug,
    )
    return {
        "slug": slug,
        "org_uuid": org_uuid,
        "root_pub": org_root.public_hex,
        "mission_id": mission_id,
        "token": token,
        "key_file": str(key_path),
        "cert_file": str(cert_path),
        "channel_cert_file": str(viewer_cert_path),
        "control_file": str(control_path),
        "mission_db": str(mission_db),
    }


def revoke(payload: dict) -> dict:
    if set(payload) != {"slug", "password", "registry_url", "token"}:
        raise FixtureError("revoke fields are malformed")
    from deploy.harness.fixture_ops import _org_root, _personal_seed

    root = _org_root(payload["slug"], _personal_seed(payload["password"]))
    path = f"/v1/links/{payload['token']}"
    result = _request(
        payload["registry_url"].rstrip("/"), "DELETE", path, root, {}
    )
    if result["status"] not in (200, 404):
        raise FixtureError(f"production link revoke refused: {result['status']}")
    return {"revoked": result["status"] == 200}


COMMANDS = {"setup": setup, "revoke": revoke}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    try:
        result = COMMANDS[args.command](_input())
    except (FixtureError, KeyError, OSError, ValueError) as exc:
        print(f"fixture failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
