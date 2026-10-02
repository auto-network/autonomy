"""auto-26e8a: organization-shared inference accounts are rows of the
organization's own vault (autonomy.org.vault.harness-credential, D10;
auto-raepo). The set is
organization-homed, raw-band and audited-vaulted, so the production sealer
seals it to the organization's key generations: every current member opens a
shared account, a member removed from the organization cannot open the next
generation, and a member admitted later is granted the head.

The member harness and its helpers mirror test_org_revocation_end_to_end.py,
which proves the same sealer on a test-only set."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tools.graph.schemas import registry
from tools.graph.schemas.harness_account import ORG_HARNESS_CREDENTIAL_SET_ID as SET_ID
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.vault.key_holder import VaultKeyCache, _scoped_db, build_key_holder
from tools.vault.key_sealer import build_vault_sealer
from tools.vault.storage_object import open_revision
from tools.vault.unlock import open_generation_keys

_SK = Path(__file__).resolve().parents[2] / "network" / "storagekit" / "tests"
if "sk_org_conftest" not in sys.modules:
    sys.path.insert(0, str(_SK))
    _spec = importlib.util.spec_from_file_location("sk_org_conftest", _SK / "conftest.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["sk_org_conftest"] = _mod
    _spec.loader.exec_module(_mod)
World = sys.modules["sk_org_conftest"].World

ORG = "acme"
KEY = "claude:org-A"


def _ledger_provider(world):
    def provider(_set_id, _org):
        return world.fold(), lambda heads: world.fold(heads=list(heads)), world.ancestry
    return provider


def _member_reads(locator, kem_private):
    with KeyControlStore(_scoped_db(SET_ID, ORG)) as kc:
        gens = open_generation_keys(kem_private, kc.accepted_grants(), kc.states)
    cache = VaultKeyCache()
    for state_id, generation_key in gens.items():
        cache.add(state_id, generation_key)
    control = build_key_holder(cache)(set_id=SET_ID, org=ORG)
    try:
        return open_revision(locator, holdings=control.holdings,
                             content_store=control.content_store)
    except Exception:
        return None


def _grant_head(world, grantor, credential, cache, state):
    from tools.network.storagekit import distribution

    with KeyControlStore(_scoped_db(SET_ID, ORG)) as kc:
        descriptor = kc.states[state]
    grant = distribution.grant_current_head(
        grantor, descriptor.domain_id, credential, descriptor,
        cache.secrets[state], world.frontier())
    with KeyControlStore(_scoped_db(SET_ID, ORG)) as kc:
        kc.accept_grant(grant)


def test_the_set_is_an_organization_audited_vault():
    assert registry.declared_home(SET_ID) == "organization"
    assert registry.declared_vault_tier(SET_ID) == "audited"


def test_members_share_an_account_and_a_removed_member_loses_the_next_generation(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)

    world = World(member_count=1)
    alice = world.member(0)
    alice_kem = world.principals[alice.public_hex]["kem_private"]
    with KeyControlStore(_scoped_db(SET_ID, ORG)) as kc:
        kc.accept_credential(world.principals[alice.public_hex]["credential"])
    cache = VaultKeyCache()
    seal = build_vault_sealer(cache, lambda org: alice, _ledger_provider(world))

    loc1 = seal(set_id=SET_ID, schema_revision=1, key=KEY, setting_id="s-1",
                payload={"harness": "claude", "access": "at-1"}, tier="audited", org=ORG)
    head = next(iter(cache.secrets))

    bob = world.admit(seed_index=40)
    bob_kem = world.principals[bob.public_hex]["kem_private"]
    with KeyControlStore(_scoped_db(SET_ID, ORG)) as kc:
        kc.accept_credential(world.principals[bob.public_hex]["credential"])
    _grant_head(world, alice, world.principals[bob.public_hex]["credential"], cache, head)

    # Both members open the shared account the first one added.
    assert _member_reads(loc1, alice_kem) == {"harness": "claude", "access": "at-1"}
    assert _member_reads(loc1, bob_kem) == {"harness": "claude", "access": "at-1"}

    # Bob leaves; the account's next revision is a generation he is not granted.
    world.remove(bob)
    loc2 = seal(set_id=SET_ID, schema_revision=1, key=KEY, setting_id="s-2",
                payload={"harness": "claude", "access": "at-2"}, tier="audited", org=ORG)
    assert _member_reads(loc2, alice_kem) == {"harness": "claude", "access": "at-2"}
    assert _member_reads(loc2, bob_kem) != {"harness": "claude", "access": "at-2"}
