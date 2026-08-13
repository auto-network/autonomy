"""B4b: a Docker-path invite join survives a real process restart."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from tools.dashboard import claim_service
from tools.data_paths import REFUSE_REAL_DATA_FALLBACK_ENV, STORE_MANIFEST
from tools.network.idkit import KeyPair, generate_token
from tools.network.invitation import Invitation, encode_invitation
from tools.network.ledger import (
    HLC,
    LedgerStore,
    make_event,
    org_ledger_db_path,
    sign_approval,
)
from tools.network.ledger.found import found_org_ledger

PASSWORD = "restart-proof personal password"
DRIVER = Path(__file__).with_name("join_process_driver.py")


@contextmanager
def _org_root(root: Path, slug: str | None = None):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    saved = {
        "AUTONOMY_ORGS_DIR": os.environ.get("AUTONOMY_ORGS_DIR"),
        "GRAPH_ORG": os.environ.get("GRAPH_ORG"),
    }
    os.environ["AUTONOMY_ORGS_DIR"] = str(root)
    if slug is None:
        os.environ.pop("GRAPH_ORG", None)
    else:
        os.environ["GRAPH_ORG"] = slug
    try:
        yield
    finally:
        GraphDB.close_all_pooled()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _node_volume(root: Path):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    saved = {
        store.env: os.environ.get(store.env)
        for store in STORE_MANIFEST
        if store.env
    }
    saved[REFUSE_REAL_DATA_FALLBACK_ENV] = os.environ.get(
        REFUSE_REAL_DATA_FALLBACK_ENV
    )
    os.environ[REFUSE_REAL_DATA_FALLBACK_ENV] = "1"
    for store in STORE_MANIFEST:
        if store.env:
            os.environ[store.env] = str(root / store.relative)
    try:
        yield
    finally:
        GraphDB.close_all_pooled()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DirectTransport:
    """Same in-process transport contract as the subprocess driver."""

    def __init__(self, world):
        self.world = world

    def request(self, payload: dict) -> dict:
        with _org_root(self.world.root, self.world.slug):
            if payload["op"] == "context":
                reply = claim_service.context(
                    self.world.slug, self.world.invitation.invite_ref
                )
                if reply.get("status") == "ok":
                    reply = {
                        **reply,
                        "org": self.world.invitation.org,
                        "root_pub": self.world.invitation.root_pub,
                    }
            elif payload["op"] == "submit":
                reply = claim_service.submit(
                    self.world.slug, payload["event"]
                )
            elif payload["op"] == "status":
                reply = claim_service.status(
                    self.world.slug,
                    self.world.invitation.invite_ref,
                    payload["persona_pub"],
                )
            else:
                raise AssertionError(payload)
        return {"v": 1, **reply}


class FoundedInvite:
    """A real founded org with one admin-ack bearer invitation."""

    def __init__(self, root: Path, slug: str):
        from tools.graph.db import GraphDB
        from tools.graph.models import Source

        self.root = root
        self.slug = slug
        self.org_id = str(uuid4())
        self.root_key = KeyPair.generate()
        self.owner_seed = os.urandom(32)
        now = int(time.time() * 1000)
        with _org_root(root, slug):
            GraphDB.create_org_db(slug, org_id=self.org_id, root=root).close()
            with LedgerStore(org_ledger_db_path(slug)) as store:
                founded = found_org_ledger(
                    store,
                    org_id=self.org_id,
                    org_root=self.root_key,
                    personal_root_seed=self.owner_seed,
                    now=now - 120_000,
                )
                self.genesis_id = founded.genesis_id
                self._ts = now - 100_000
                self._store = store
                self._emit({
                    "type": "role.define",
                    "name": "member",
                    "scope_set": ["graph:read"],
                    "claim_requires": "admin-ack",
                    "version": 1,
                })
                self.admin = KeyPair.generate()
                self._emit({
                    "type": "delegate",
                    "child_pub": self.admin.public_hex,
                    "scope": ["role:grant:member"],
                    "can_redelegate": False,
                })
                claim_token = generate_token()
                channel_token = generate_token()
                while channel_token == claim_token:
                    channel_token = generate_token()
                self.expiry = now + 3_600_000
                invite_ref = self._emit({
                    "type": "invite",
                    "granted_role": "member",
                    "expiry": self.expiry,
                    "sponsor": self.root_key.public_hex,
                    "token_hash": hashlib.sha256(
                        claim_token.encode()
                    ).hexdigest(),
                })
            db = GraphDB(org_ledger_db_path(slug))
            try:
                self.source = Source(
                    type="note",
                    platform="local",
                    title="Welcome to the joined organization",
                    file_path=f"note:{uuid4()}",
                    metadata={"body": "content readable after admission"},
                )
                db.insert_source(self.source)
            finally:
                db.close()

        self.invitation = Invitation(
            org=self.org_id,
            root_pub=self.root_key.public_hex,
            invite_ref=invite_ref,
            channel_token=channel_token,
            claim_token=claim_token,
        )
        self.code = encode_invitation(self.invitation)

    def _emit(self, payload: dict) -> str:
        self._ts += 1_000
        return self._store.append(make_event(
            self.root_key,
            payload,
            sorted(self._store.heads()),
            HLC(self._ts),
        ))

    def approve(self, persona_pub: str) -> dict:
        with _org_root(self.root, self.slug):
            with LedgerStore(org_ledger_db_path(self.slug)) as store:
                key = store.claim_key(
                    self.invitation.invite_ref, persona_pub
                )
                pending = store.get_pending_claim(key)
                assert pending is not None
                approval = sign_approval(
                    self.admin, "member.claim", pending["body"]
                )
            return claim_service.countersign(
                self.slug,
                self.invitation.invite_ref,
                persona_pub,
                approval,
            )

    def member_and_content(self, persona_pub: str) -> tuple[object, dict]:
        from tools.graph.db import GraphDB

        with _org_root(self.root, self.slug):
            with LedgerStore(org_ledger_db_path(self.slug)) as store:
                member = store.fold().members[persona_pub]
            db = GraphDB(org_ledger_db_path(self.slug))
            try:
                source = db.get_source(self.source.id)
            finally:
                db.close()
        return member, source


def _run_node(
    volume: Path,
    world: FoundedInvite,
    password_file: Path,
    *,
    wall_clock_ms: int | None = None,
) -> dict:
    proc = _run_node_process(
        volume,
        world,
        password_file,
        wall_clock_ms=wall_clock_ms,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.splitlines()[-1])


def _run_node_process(
    volume: Path,
    world: FoundedInvite,
    password_file: Path,
    *,
    wall_clock_ms: int | None = None,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(DRIVER),
        "--volume",
        str(volume),
        "--remote-orgs",
        str(world.root),
        "--remote-slug",
        world.slug,
        "--invite",
        world.code,
        "--password-file",
        str(password_file),
    ]
    if wall_clock_ms is not None:
        command.extend(["--wall-clock-ms", str(wall_clock_ms)])
    return subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=60,
    )


def _personal_armor(volume: Path) -> str:
    # The volume contract explicitly pins GRAPH_DB, and settings_ops treats
    # that as the operator-selected Settings store. The personal org DB still
    # exists as the join branch's only org database; the armor row is in the
    # pinned Settings DB by that resolver contract.
    with sqlite3.connect(volume / "graph.db") as conn:
        row = conn.execute(
            "SELECT payload FROM settings "
            "WHERE set_id = 'autonomy.identity.personal'"
        ).fetchone()
    assert row is not None
    return json.loads(row[0])["armored_private_key"]


def test_docker_path_pending_join_survives_a_process_restart(tmp_path):
    """Fresh Docker/data-root boot stages, restarts, finalizes, then reads."""
    world = FoundedInvite(tmp_path / "remote-orgs", "inviting")
    volume = tmp_path / "node-volume"
    password_file = tmp_path / "personal-password"
    password_file.write_text(PASSWORD + "\n")

    first = _run_node(volume, world, password_file)
    assert first["org_dbs"] == ["personal.db"]
    assert len(first["pending"]) == 1
    pending = first["pending"][0]
    assert pending["invite_ref"] == world.invitation.invite_ref
    assert pending["claim_key"] == hashlib.sha256(
        (pending["invite_ref"] + pending["persona_pub"]).encode("ascii")
    ).hexdigest()
    assert (pending["have"], pending["need"]) == (0, 1)
    with sqlite3.connect(volume / "pending_joins.db") as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(pending_joins)")
        }
    assert columns == {
        "org",
        "invite_ref",
        "persona_pub",
        "claim_key",
        "have",
        "need",
        "created_at",
        "updated_at",
    }

    # The restart locator is structurally and byte-wise secret-free.
    pending_bytes = (volume / "pending_joins.db").read_bytes()
    for secret in (
        world.invitation.channel_token.encode(),
        world.invitation.claim_token.encode(),
        PASSWORD.encode(),
        b"armored_private_key",
    ):
        assert secret not in pending_bytes
    armor_before = _personal_armor(volume)

    ready = world.approve(pending["persona_pub"])
    assert ready["status"] == "ready"
    assert ready["admitting"] == [world.admin.public_hex]

    # A second OS process resumes via status, uses the server's pinned
    # position/admitting subset, and deletes the row only after append.
    second = _run_node(
        volume,
        world,
        password_file,
        # context() would return invite-expired at this clock. A successful
        # restart proves the durable row selected status-first resume and the
        # pinned cz4fb finalize exemption survived the process boundary.
        wall_clock_ms=world.expiry + 1,
    )
    assert second["org_dbs"] == ["personal.db"]
    assert second["pending"] == []
    assert _personal_armor(volume) == armor_before

    member, source = world.member_and_content(pending["persona_pub"])
    assert member.roles == ("member",)
    assert source["title"] == "Welcome to the joined organization"
    assert json.loads(source["metadata"])["body"] == (
        "content readable after admission"
    )


def test_restart_refuses_when_the_seed_no_longer_derives_the_staged_persona(
    tmp_path,
):
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit.armor import encrypt_root_key

    world = FoundedInvite(tmp_path / "remote-orgs", "inviting")
    volume = tmp_path / "node-volume"
    password_file = tmp_path / "personal-password"
    password_file.write_text(PASSWORD + "\n")
    first = _run_node(volume, world, password_file)
    assert len(first["pending"]) == 1

    # Replace the local armor with another valid personal root. The server's
    # staged persona remains the original one; continuity must refuse before
    # any finalize, even though the replacement armor unlocks successfully.
    replacement = KeyPair.generate()
    with _node_volume(volume):
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                PERSONAL_IDENTITY_SET_ID,
                1,
                "default",
                {
                    "armored_private_key": encrypt_root_key(
                        replacement, PASSWORD
                    ),
                    "root_pub": replacement.public_hex,
                    "display_name": "Wrong identity",
                    "created_at": "2026-07-26T00:00:00Z",
                },
                org=None,
            )
    refused = _run_node_process(
        volume,
        world,
        password_file,
        wall_clock_ms=world.expiry + 1,
    )
    assert refused.returncode != 0
    assert "does not match the staged join persona" in refused.stderr
    assert world.invitation.channel_token not in refused.stderr
    assert world.invitation.claim_token not in refused.stderr
    with sqlite3.connect(volume / "pending_joins.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_joins"
        ).fetchone()[0] == 1


@pytest.fixture
def identified_node(tmp_path, monkeypatch):
    volume = tmp_path / "identified-node"
    world = FoundedInvite(tmp_path / "remote-orgs", "inviting")

    with _node_volume(volume):
        from tools.graph import org_ops
        from tools.init.join import _mint_personal_identity

        org_ops.ensure_bootstrap_orgs(
            root=volume / "orgs", first_org=None, personal_only=True
        )
        _mint_personal_identity(PASSWORD, display_name="Existing operator")
        armor = _personal_armor(volume)
        monkeypatch.setattr(
            "tools.init.join.production_transport",
            lambda invitation: DirectTransport(world),
        )
        yield volume, world, armor


def test_resume_refuses_a_different_org_genesis(identified_node):
    from tools.init.join import (
        JoinError,
        join_existing_identity,
        persist_outcome,
    )

    volume, world, _armor = identified_node
    other = FoundedInvite(world.root.parent / "other-orgs", "other")
    direct = DirectTransport(world)

    class SwappedGenesis:
        def __init__(self):
            self.operations = []

        def request(self, payload):
            self.operations.append(payload["op"])
            reply = direct.request(payload)
            if payload["op"] == "status" and reply.get("status") == "pending":
                return {**reply, "genesis_id": other.genesis_id}
            return reply

    with _node_volume(volume):
        initial = join_existing_identity(
            world.invitation, direct, password=PASSWORD
        )
        assert initial.state == "pending"
        persist_outcome(initial)

        swapped = SwappedGenesis()
        with pytest.raises(
            JoinError, match="does not match the staged join persona"
        ):
            join_existing_identity(
                world.invitation, swapped, password=PASSWORD
            )
        assert swapped.operations == ["status"]
