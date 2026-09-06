"""First-run's JOIN branch: AUTONOMY_INVITE as a peer of AUTONOMY_FIRST_ORG."""

from __future__ import annotations

import pytest

from tools.data_paths import REFUSE_REAL_DATA_FALLBACK_ENV, STORE_MANIFEST
from tools.init.first_run import CREATED, FAILED, PENDING, InitConflict, initialize
from tools.network.invitation import Invitation, InvitationError, encode_invitation
from tools.network import fleet_invite
from tools.network.idkit import KeyPair

ORG = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
ROOT_PUB = "ab" * 32
INVITE_REF = "cd" * 32
GRANT_TOKEN = "12" * 16
CLAIM_TOKEN = "ef" * 32


def code() -> str:
    return encode_invitation(
        Invitation(
            org=ORG,
            root_pub=ROOT_PUB,
            invite_ref=INVITE_REF,
            channel_token=GRANT_TOKEN,
            claim_token=CLAIM_TOKEN,
        )
    )


def fleet_code() -> str:
    root = KeyPair.from_private_hex("34" * 32)
    return fleet_invite.encode(fleet_invite.mint(
        root,
        rendezvous=f"https://relay.auto.network/l/{GRANT_TOKEN}",
        invite_id="78" * 32,
        expires_at=4_102_444_800_000,
    ))


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A fresh volume, rooted per the B1 contract with the guard armed so a
    store we forgot to root raises rather than touching real data/."""
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    for store in STORE_MANIFEST:
        if store.key == "graph":
            # Never pinned: a GRAPH_DB pin conflicts with the join flow's
            # explicit org='personal' writes; the ambient base below roots
            # graph.db at the same path without collapsing org routing.
            monkeypatch.delenv(store.env, raising=False)
            continue
        if store.env:
            monkeypatch.setenv(store.env, str(tmp_path / "data" / store.relative))
    from tools.data_paths import DATA_ROOT_ENV

    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "data"))
    monkeypatch.delenv("AUTONOMY_FIRST_ORG", raising=False)
    monkeypatch.delenv("AUTONOMY_INVITE", raising=False)
    monkeypatch.delenv("AUTONOMY_FLEET_INVITE", raising=False)
    return tmp_path


def _step(report, name):
    return next((s for s in report.steps if s.name == name), None)


def test_invite_branch_founds_no_shared_org(volume):
    """Joining means membership arrives from the INVITING org's ledger —
    a node that also founded its own would have two identities."""
    report = initialize(volume, invite=code(), tls=False)
    orgs = {p.stem for p in (volume / "data" / "orgs").glob("*.db")}
    assert orgs == set(), f"join path created a shared org: {orgs}"
    # The personal store is not an organization; it lands beside orgs/
    # (auto-35kmy).
    assert (volume / "data" / "personal.db").exists()
    assert _step(report, "org:personal").action == CREATED
    join = _step(report, "join")
    assert join.action == PENDING


def test_create_branch_still_founds(volume):
    report = initialize(volume, first_org="acme", tls=False)
    orgs = {p.stem for p in (volume / "data" / "orgs").glob("*.db")}
    assert orgs == {"acme"}
    assert (volume / "data" / "personal.db").exists()
    assert _step(report, "join") is None


def test_found_and_join_together_is_a_hard_error(volume):
    """Silently preferring one would strand state under the other."""
    with pytest.raises(InitConflict):
        initialize(volume, first_org="acme", invite=code(), tls=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"first_org": "acme", "fleet_invite": "fleet"},
        {"invite": "org", "fleet_invite": "fleet"},
    ],
)
def test_fleet_invite_is_an_exclusive_bootstrap_mode(volume, kwargs):
    with pytest.raises(InitConflict, match="exactly one first-run bootstrap"):
        initialize(volume, tls=False, **kwargs)


def test_a_bad_code_fails_first_run_before_any_network_call(volume):
    with pytest.raises(InvitationError):
        initialize(volume, invite="not-an-invitation", tls=False)


def test_the_report_never_carries_the_bearer(volume):
    """The report is printed to container logs and emitted as JSON."""
    report = initialize(volume, invite=code(), tls=False)
    rendered = repr(report) + str(_step(report, "join").detail)
    assert GRANT_TOKEN not in rendered
    assert CLAIM_TOKEN not in rendered
    assert ORG in rendered  # the org IS identified: redaction, not obscurity


def test_join_branch_writes_only_inside_the_volume(volume):
    """The B1 contract holds on the join path too."""
    initialize(volume, invite=code(), tls=False)
    assert (volume / "data" / "personal.db").exists()
    assert not (volume / "data" / "graph.db").exists()


def test_first_run_runs_the_join_ceremony_when_a_transport_is_given(volume, tmp_path):
    """End of the B4a path: AUTONOMY_INVITE on a fresh volume boots into
    the ceremony and reports its outcome, with no password source so it
    stages (ruling (c)) rather than minting."""
    from tools.init.first_run import initialize

    class FakeOrg:
        def request(self, payload):
            assert payload["op"] == "context"
            return {
                "status": "ok", "genesis_id": "9f" * 32, "heads": ["7e" * 32],
                "max_hlc": [1_800_000_000_000, 0], "granted_role": "member",
                "binding": "token",
            }

    report = initialize(volume, invite=code(), tls=False, join_transport=FakeOrg())
    staged = _step(report, "join:staged")
    assert staged is not None, [s.name for s in report.steps]
    assert GRANT_TOKEN not in repr(report)
    assert CLAIM_TOKEN not in repr(report)


class _FakeJoinStateStore:
    """Only what _run_fleet_join's bootstrap-once guards consult."""

    def __init__(self, saved=None, delivery=None):
        self._saved = saved
        self._delivery = delivery
        self.deleted = []

    def latest_any(self):
        return self._saved

    def load_delivery(self, request_id):
        return self._delivery

    def delete(self, request_id):
        self.deleted.append(request_id)


def test_fleet_invite_submits_one_public_machine_request(volume):
    class Recovery:
        verification_code = "A1B2 C3D4 E5F6 0718 192A 3B4C"

    class FakeFleetClient:
        def __init__(self):
            self.invites = []
            self.state_store = _FakeJoinStateStore()

        async def start_or_recover(self, invitation):
            self.invites.append(invitation)
            return Recovery()

        async def resume(self, recovery):
            assert recovery.verification_code == Recovery.verification_code
            return type("Result", (), {"status": "pending"})()

    client = FakeFleetClient()
    encoded = fleet_code()
    report = initialize(
        volume,
        fleet_invite="AUTONOMY_FLEET_INVITE=" + encoded,
        fleet_client=client,
        tls=False,
    )

    assert len(client.invites) == 1
    assert client.invites[0] == fleet_invite.decode(encoded)
    assert not list((volume / "data" / "orgs").glob("*.db"))
    assert (volume / "data" / "personal.db").exists()
    pending = _step(report, "fleet-enrollment:pending")
    assert pending.action == PENDING
    assert Recovery.verification_code in pending.detail
    rendered = repr(report)
    assert GRANT_TOKEN not in rendered
    assert "machine_id" not in rendered
    assert "machine_pub" not in rendered
