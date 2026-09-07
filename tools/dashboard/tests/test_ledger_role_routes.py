"""The role vocabulary has to be reachable from a client, and only validly.

Every organization founds with one role, ``owner``, and until these routes
existed nothing could append a ``role.define``, ``role.grant`` or
``role.revoke`` after founding: the fold and the event validators were
complete, the wire was missing. The routes share one core with the invite and
revoke routes — client-signed wire in, signature verified, heads gated,
TRIAL-FOLDED so an unauthorized change is a refusal carrying the fold's own
reason rather than an invalid row in every replica, then appended.

Roles design of record: graph://d1b3db8f-879 (bead auto-7l0ku).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import network_routes

T0 = 1_800_000_000_000


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    # AUTONOMY_ORGS_DIR outranks the data root in every store resolver, and
    # the suite's conftest pins it per worker — so without a per-TEST value
    # every founding in this file would land in one shared personal.db and
    # the second would fail with "ledger already has a genesis event".
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", "personal")
    app = Starlette(routes=[
        Route("/api/network/ledger/role-define",
              network_routes.post_ledger_role_define, methods=["POST"]),
        Route("/api/network/ledger/role-grant",
              network_routes.post_ledger_role_grant, methods=["POST"]),
        Route("/api/network/ledger/role-revoke",
              network_routes.post_ledger_role_revoke, methods=["POST"]),
    ])
    with TestClient(app) as c:
        yield c
    # This fixture repoints AUTONOMY_ORGS_DIR/AUTONOMY_DATA_ROOT at a tmp
    # dir; tools.graph.db pools connections by path, so a handle opened
    # under those paths must not survive into the next module.
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()


class Org:
    """A founded ledger plus the keys a browser would hold: the org root
    (unsealed from its armor) and the founder's persona."""

    def __init__(self):
        from tools.network.idkit import KeyPair
        from tools.network.idkit.persona import derive_persona
        from tools.network.ledger import LedgerStore, org_ledger_db_path
        from tools.network.ledger.found import found_org_ledger
        from tools.network.storagekit import credentials as cm

        self.seed = bytes(range(32))
        self.root = KeyPair.generate()
        self.path = org_ledger_db_path("personal")
        with LedgerStore(self.path) as store:
            res = found_org_ledger(
                store, org_id="personal", org_root=self.root,
                personal_root_seed=self.seed, now=T0,
                kem_seed=cm.derive_kem_seed(self.seed),
            )
            self.genesis_id = res.genesis_id
            self.size = len(store)
        self.founder = derive_persona(self.seed, self.genesis_id)
        self._tick = 0

    def heads(self):
        from tools.network.ledger import LedgerStore
        with LedgerStore(self.path) as store:
            return store.heads()

    def fold(self):
        from tools.network.ledger import LedgerStore
        with LedgerStore(self.path) as store:
            return store.fold()

    def count(self):
        from tools.network.ledger import LedgerStore
        with LedgerStore(self.path) as store:
            return len(store)

    def wire(self, author, payload, parents=None):
        """Mint a signed event at the current heads (or given parents) and
        return its canonical wire string, exactly what a browser posts."""
        from tools.network.ledger import HLC
        from tools.network.ledger.events import make_event
        self._tick += 1
        event = make_event(
            author, payload,
            list(parents) if parents is not None else list(self.heads()),
            HLC(T0 + 1000 * self._tick, 0),
        )
        return event.to_json().decode()


def define_payload(name, scope_set=(), requires="admin-ack", version=1,
                   threshold=None):
    payload = {
        "type": "role.define",
        "name": name,
        "scope_set": sorted(set(scope_set)),
        "claim_requires": requires,
        "version": version,
    }
    if threshold is not None:
        payload["approver_threshold"] = {"kind": "static", "count": threshold}
    return payload


def _post(client, route, **body):
    return client.post(f"/api/network/ledger/{route}", json=body)


# -- the plain refusals every ledger route shares --------------------------

@pytest.mark.parametrize("route", ["role-define", "role-grant", "role-revoke"])
def test_a_missing_org_is_refused(client, route):
    assert _post(client, route, event="{}").status_code == 400


@pytest.mark.parametrize("route", ["role-define", "role-grant", "role-revoke"])
def test_a_missing_event_is_refused(client, route):
    assert _post(client, route, org="personal").status_code == 400


def test_garbage_is_refused_before_anything_is_appended(client):
    r = _post(client, "role-define", org="personal", event="not an event")
    assert r.status_code == 400
    assert "rejected" in r.json()["error"]


def test_an_unfounded_org_is_404(client):
    from tools.network.idkit import KeyPair
    from tools.network.ledger import HLC
    from tools.network.ledger.events import make_event
    root = KeyPair.generate()
    # A non-genesis event needs a parent to parse at all; any well-formed id
    # will do because the route must answer "not founded" before it looks.
    event = make_event(root, define_payload("member"), ["ab" * 32], HLC(T0, 0))
    r = _post(client, "role-define", org="personal",
              event=event.to_json().decode())
    assert r.status_code == 404, r.text


def test_mock_mode_has_no_ledger(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    r = _post(client, "role-define", org="personal", event="{}")
    assert r.status_code == 502


def test_the_route_checks_the_event_kind(client):
    org = Org()
    grant = org.wire(org.root, {
        "type": "role.grant", "persona": org.founder.public_hex, "role": "owner",
    })
    r = _post(client, "role-define", org="personal", event=grant)
    assert r.status_code == 400
    assert "must be role.define" in r.json()["error"]


# -- role.define ------------------------------------------------------------

def test_a_root_signed_definition_becomes_DURABLE(client):
    """THE ONE THAT MATTERS for define: the org root is the only key that
    can define a role today, and the definition must survive a re-open so
    the mint form's picker (which reads the fold's role_defs) shows it."""
    org = Org()
    before = org.count()
    wire = org.wire(org.root, define_payload("member", threshold=1))

    r = _post(client, "role-define", org="personal", event=wire)

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["event_id"]
    assert org.count() == before + 1
    view = org.fold().role_defs["member"]
    assert view.version == 1
    assert view.scope_set == ()
    assert view.claim_requires == "admin-ack"
    assert view.approver_threshold == 1


def test_an_owner_persona_may_define_a_role_that_carries_nothing(client):
    """``attenuates`` over an EMPTY scope set is vacuously true, so a holder
    of ``role:define`` (the founder, through the owner role's ``*``) may
    define a role with no capabilities even with no delegable closure. The
    minimal Member role is therefore persona-definable; only roles that
    carry a scope need the org root (next test)."""
    org = Org()
    wire = org.wire(org.founder, define_payload("member"))

    r = _post(client, "role-define", org="personal", event=wire)

    assert r.status_code == 200, r.text
    assert org.fold().role_defs["member"].scope_set == ()


def test_an_owner_persona_cannot_define_a_scoped_role_and_learns_why(client):
    """Role-held scopes are never delegable: the founder holds ``*`` through
    the owner role yet has no delegable closure, so a definition carrying any
    scope is refused with role-define-overreach — and the route says so
    instead of appending."""
    org = Org()
    before = org.count()
    wire = org.wire(org.founder, define_payload("admin", scope_set=["invite:member"]))

    r = _post(client, "role-define", org="personal", event=wire)

    assert r.status_code == 403, r.text
    assert r.json()["reason"] == "role-define-overreach"
    assert org.count() == before, "a refused definition must not be appended"


def test_a_definition_at_stale_heads_is_409(client):
    org = Org()
    stale = org.heads()
    # Something lands first (root defines one role), so the heads move on.
    first = org.wire(org.root, define_payload("member"))
    assert _post(client, "role-define", org="personal", event=first).status_code == 200
    late = org.wire(org.root, define_payload("admin"), parents=stale)

    r = _post(client, "role-define", org="personal", event=late)

    assert r.status_code == 409
    assert "advanced" in r.json()["error"]


def test_versions_are_contiguous_per_name(client):
    org = Org()
    # A brand-new name must start at 1.
    r = _post(client, "role-define", org="personal",
              event=org.wire(org.root, define_payload("member", version=2)))
    assert r.status_code == 400 and r.json()["expected_version"] == 1

    assert _post(client, "role-define", org="personal",
                 event=org.wire(org.root, define_payload("member"))).status_code == 200

    # Replaying version 1 is refused; the next version is 2.
    r = _post(client, "role-define", org="personal",
              event=org.wire(org.root, define_payload("member", version=1)))
    assert r.status_code == 400 and r.json()["expected_version"] == 2

    r = _post(client, "role-define", org="personal",
              event=org.wire(org.root, define_payload(
                  "member", scope_set=["invite:member"], version=2)))
    assert r.status_code == 200, r.text
    assert org.fold().role_defs["member"].scope_set == ("invite:member",)


# -- role.grant and role.revoke ---------------------------------------------

def test_a_holder_of_role_grant_confers_the_role_and_a_stranger_cannot(client):
    from tools.network.idkit import KeyPair
    org = Org()
    assert _post(client, "role-define", org="personal",
                 event=org.wire(org.root, define_payload("member"))).status_code == 200
    target = KeyPair.generate().public_hex

    # The founder holds ``*`` through the owner role, which covers
    # role:grant:member — a grant is a use of held authority, not a
    # delegation, so no closure is needed.
    r = _post(client, "role-grant", org="personal", event=org.wire(
        org.founder, {"type": "role.grant", "persona": target, "role": "member"}))
    assert r.status_code == 200, r.text
    assert org.fold().roles(target) == ("member",)

    stranger = KeyPair.generate()
    before = org.count()
    r = _post(client, "role-grant", org="personal", event=org.wire(
        stranger, {"type": "role.grant", "persona": target, "role": "member"}))
    assert r.status_code == 403
    assert r.json()["reason"] == "role-grant-unauthorized"
    assert org.count() == before


def test_granting_an_undefined_role_is_refused_before_the_fold(client):
    from tools.network.idkit import KeyPair
    org = Org()
    r = _post(client, "role-grant", org="personal", event=org.wire(
        org.root, {"type": "role.grant",
                   "persona": KeyPair.generate().public_hex, "role": "ghost"}))
    assert r.status_code == 400
    assert "not defined" in r.json()["error"]


def test_revoke_by_root_strips_the_role_and_a_stranger_cannot(client):
    from tools.network.idkit import KeyPair
    org = Org()
    assert _post(client, "role-define", org="personal",
                 event=org.wire(org.root, define_payload("member"))).status_code == 200
    target = KeyPair.generate().public_hex
    assert _post(client, "role-grant", org="personal", event=org.wire(
        org.root, {"type": "role.grant", "persona": target, "role": "member"}),
    ).status_code == 200
    assert org.fold().roles(target) == ("member",)

    stranger = KeyPair.generate()
    r = _post(client, "role-revoke", org="personal", event=org.wire(
        stranger, {"type": "role.revoke", "persona": target, "role": "member"}))
    assert r.status_code == 403
    assert r.json()["reason"] == "role-revoke-unauthorized"

    r = _post(client, "role-revoke", org="personal", event=org.wire(
        org.root, {"type": "role.revoke", "persona": target, "role": "member"}))
    assert r.status_code == 200, r.text
    assert org.fold().roles(target) == ()
