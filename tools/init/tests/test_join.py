"""Join orchestration: anchor pinning, the password ruling, the claim."""

from __future__ import annotations

import os

import pytest

from tools.data_paths import REFUSE_REAL_DATA_FALLBACK_ENV, STORE_MANIFEST
from tools.init.join import (
    ADMITTED,
    LEGACY_PASSWORD_FILE_ENV,
    PENDING,
    STAGED,
    TEST_AUTOMATION_ENV,
    TEST_PASSWORD_FILE_ENV,
    AnchorMismatch,
    JoinError,
    ViewerJoinTransport,
    join_org,
    read_personal_password,
    verify_anchor,
)
from tools.network.invitation import Invitation

ORG = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
OTHER_ORG = "018f6b2a-7c4d-7e11-8a3b-000000000000"
ROOT_PUB = "ab" * 32
INVITE_REF = "cd" * 32
GRANT_TOKEN = "12" * 16
CLAIM_TOKEN = "ef" * 32
GENESIS = "9f" * 32
PASSWORD = "mounted-secret-passphrase"


def invitation() -> Invitation:
    return Invitation(
        org=ORG,
        root_pub=ROOT_PUB,
        invite_ref=INVITE_REF,
        channel_token=GRANT_TOKEN,
        claim_token=CLAIM_TOKEN,
    )


class FakeOrg:
    """An in-process stand-in for the org node at the far end of the
    channel. The orchestration under test is the real one; only the
    transport is substituted (the 4d6qm shape)."""

    def __init__(self, submit_status="pending", context_over=None):
        self.submit_status = submit_status
        self.context_over = context_over or {}
        self.requests = []

    def request(self, payload: dict) -> dict:
        self.requests.append(payload)
        if payload["op"] == "context":
            return {
                "status": "ok", "genesis_id": GENESIS, "heads": ["7e" * 32],
                "max_hlc": [1_800_000_000_000, 0], "granted_role": "member",
                "binding": "token", "invite_expiry": 1_900_000_000_000,
                **self.context_over,
            }
        if payload["op"] == "submit":
            if self.submit_status == "admitted":
                return {"status": "admitted", "kem_credential": None}
            if self.submit_status == "rejected":
                return {"status": "rejected", "reason": "invite-expired"}
            return {"status": "pending", "have": 0, "need": 1}
        raise AssertionError(f"unexpected op {payload['op']}")


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    for store in STORE_MANIFEST:
        if store.key == "graph":
            # Deliberately NOT pinned — the dashboard conftest's rule: a
            # GRAPH_DB pin conflicts with every explicit-org settings write
            # under the fail-loud resolver (org='personal' writes are part
            # of the join/bootstrap flow itself).
            continue
        if store.env:
            monkeypatch.setenv(store.env, str(tmp_path / "data" / store.relative))
    (tmp_path / "data").mkdir(exist_ok=True)
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    (tmp_path / "data" / "orgs").mkdir(parents=True, exist_ok=True)
    GraphDB.create_org_db("personal", type_="personal",
                          root=tmp_path / "data" / "orgs").close()
    monkeypatch.delenv(LEGACY_PASSWORD_FILE_ENV, raising=False)
    monkeypatch.delenv(TEST_AUTOMATION_ENV, raising=False)
    monkeypatch.delenv(TEST_PASSWORD_FILE_ENV, raising=False)
    yield tmp_path
    GraphDB.close_all_pooled()


# -- the password source: the ruling, enforced --------------------------------------


def test_password_never_comes_from_the_environment(monkeypatch):
    """The personal root unlocks every persona in every org this identity
    ever joins; an env var would keep it in docker inspect for the node's
    life. Production accepts only one-time stdin."""
    monkeypatch.setenv("AUTONOMY_PERSONAL_PASSWORD", "should-be-ignored")
    monkeypatch.delenv(LEGACY_PASSWORD_FILE_ENV, raising=False)
    monkeypatch.delenv(TEST_PASSWORD_FILE_ENV, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # no stdin source
    assert read_personal_password() is None


def test_legacy_production_password_file_is_refused(tmp_path, monkeypatch):
    secret = tmp_path / "personal-password"
    secret.write_text(PASSWORD + "\n")
    monkeypatch.setenv(LEGACY_PASSWORD_FILE_ENV, str(secret))
    with pytest.raises(JoinError, match="not a supported production input"):
        read_personal_password()


def test_password_file_requires_both_test_automation_gates(tmp_path, monkeypatch):
    secret = tmp_path / "personal-password"
    secret.write_text(PASSWORD + "\n")
    monkeypatch.setenv(TEST_PASSWORD_FILE_ENV, str(secret))
    with pytest.raises(JoinError, match="TEST AUTOMATION ONLY"):
        read_personal_password()

    monkeypatch.setenv(TEST_AUTOMATION_ENV, "1")
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    assert read_personal_password() == PASSWORD


def test_unreadable_or_empty_password_file_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv(TEST_AUTOMATION_ENV, "1")
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    monkeypatch.setenv(TEST_PASSWORD_FILE_ENV, str(tmp_path / "nope"))
    with pytest.raises(JoinError):
        read_personal_password()
    empty = tmp_path / "empty"
    empty.write_text("\n")
    monkeypatch.setenv(TEST_PASSWORD_FILE_ENV, str(empty))
    with pytest.raises(JoinError):
        read_personal_password()


# -- anchor pinning ------------------------------------------------------------------


def test_anchor_mismatch_refuses_the_join():
    """A transport that reached a different org must not pass its context
    off as the invited org's."""
    with pytest.raises(AnchorMismatch):
        verify_anchor({"org": OTHER_ORG}, invitation())
    with pytest.raises(AnchorMismatch):
        verify_anchor({"root_pub": "99" * 32}, invitation())
    verify_anchor({"org": ORG, "root_pub": ROOT_PUB}, invitation())  # matching: fine


def test_join_refuses_a_wrong_org_context(volume):
    org = FakeOrg(context_over={"org": OTHER_ORG})
    with pytest.raises(AnchorMismatch):
        join_org(invitation(), org, password=PASSWORD)


def test_a_dead_invitation_is_reported_not_claimed(volume):
    class Gone(FakeOrg):
        def request(self, payload):
            return {"status": "gone", "reason": "invite-expired"}

    with pytest.raises(JoinError, match="no longer usable"):
        join_org(invitation(), Gone(), password=PASSWORD)


# -- the two ruled paths --------------------------------------------------------------


def test_no_password_stages_and_mints_nothing(volume):
    """Ruling (c): validate against the live org, mint NO identity, leave
    identity setup to the dashboard's local prompt."""
    org = FakeOrg()
    outcome = join_org(invitation(), org, password=None)
    assert outcome.state == STAGED
    assert outcome.granted_role == "member"
    assert outcome.persona_pub is None
    # It verified against the org but never submitted a claim.
    assert [r["op"] for r in org.requests] == ["context"]
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID

    members = settings_ops.read_owned_set(PERSONAL_IDENTITY_SET_ID, org=None).members
    assert members == [] or all(not m.payload for m in members)


def test_with_a_password_mints_locally_and_claims(volume):
    """Ruling (b): headless mint, then the real membership claim flow."""
    org = FakeOrg(submit_status="pending")
    outcome = join_org(invitation(), org, password=PASSWORD)
    assert outcome.state == PENDING  # bearer safety: token claims stage
    assert (outcome.have, outcome.need) == (0, 1)
    assert outcome.persona_pub

    # The persona is derived from the ORG's real genesis, not the invitation.
    from tools.network.idkit import derive_persona
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit.armor import decrypt_root_key

    member = [
        m for m in settings_ops.read_owned_set(PERSONAL_IDENTITY_SET_ID, org=None).members
        if isinstance(m.payload, dict)
    ][0]
    seed = bytes.fromhex(decrypt_root_key(member.payload["armored_private_key"],
                                          PASSWORD).private_hex)
    assert derive_persona(seed, GENESIS).public_hex == outcome.persona_pub

    # The claim actually carried the bearer to the ORG (over the channel).
    submit = [r for r in org.requests if r["op"] == "submit"][0]
    assert CLAIM_TOKEN in submit["event"]
    assert GRANT_TOKEN not in submit["event"]


def test_admission_is_reported(volume):
    outcome = join_org(invitation(), FakeOrg(submit_status="admitted"),
                       password=PASSWORD)
    assert outcome.state == ADMITTED


def test_a_rejected_claim_raises_rather_than_reporting_success(volume):
    with pytest.raises(JoinError, match="invite-expired"):
        join_org(invitation(), FakeOrg(submit_status="rejected"), password=PASSWORD)


def test_outcome_never_carries_the_bearer_or_the_password(volume):
    outcome = join_org(invitation(), FakeOrg(), password=PASSWORD)
    rendered = repr(outcome)
    assert GRANT_TOKEN not in rendered
    assert CLAIM_TOKEN not in rendered
    assert PASSWORD not in rendered


def test_production_transport_pins_the_invitation_and_redacts_the_bearer(
    monkeypatch,
):
    """The production seam is the real ViewerChannel, pinned from the code."""
    from tools.network.idkit import canonical_json
    from tools.network.relaykit import viewer

    seen = {}

    class Channel:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, payload):
            seen["payload"] = payload

        async def recv_message(self):
            return b'{"v":1,"status":"absent"}'

    async def connect(relay_url, token, **kwargs):
        seen.update(relay_url=relay_url, token=token, kwargs=kwargs)
        return Channel()

    monkeypatch.setattr(viewer.ViewerChannel, "connect", connect)
    transport = ViewerJoinTransport(
        invitation(), relay_url="wss://relay.example", timeout=1
    )
    assert transport.request(
        {"v": 1, "op": "status", "persona_pub": "12" * 32}
    ) == {"v": 1, "status": "absent"}
    assert seen == {
        "relay_url": "wss://relay.example",
        "token": GRANT_TOKEN,
        "kwargs": {
            "root_pub": ROOT_PUB,
            "org": ORG,
            "open_timeout": 1,
        },
        "payload": canonical_json(
            {"v": 1, "op": "status", "persona_pub": "12" * 32}
        ),
    }
    assert CLAIM_TOKEN not in repr(seen)
    assert GRANT_TOKEN not in repr(transport)
    assert CLAIM_TOKEN not in repr(transport)


def test_production_transport_does_not_leak_a_token_bearing_failure(monkeypatch):
    from tools.network.relaykit import viewer

    async def connect(*_args, **_kwargs):
        raise OSError(
            f"could not open /v1/links/{GRANT_TOKEN}/channel "
            f"with accidental bearer {CLAIM_TOKEN}"
        )

    monkeypatch.setattr(viewer.ViewerChannel, "connect", connect)
    transport = ViewerJoinTransport(
        invitation(), relay_url="wss://relay.example", timeout=1
    )
    with pytest.raises(JoinError) as caught:
        transport.request({"v": 1, "op": "context"})
    assert GRANT_TOKEN not in str(caught.value)
    assert CLAIM_TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is not None
    assert caught.value.__suppress_context__
