"""End-to-end R6 proof: a kicked-out member cannot read a rewritten org secret.

Founds an organization, invites a second member, vaults a secret through the
REAL production sealer, proves both members can read it, removes one member,
rewrites the secret, and proves only the remaining member can read the new
value.

This is the crib's R6 (``1e005d5c-c11`` §revocation, line 167): a generation's
grant goes to the *current domain principals'* current credentials, so a
removed member gets no grant for the generation minted after their removal and
cannot open the rewritten secret. If the sealer instead grants to every
credential ever stored (``accepted_credentials()``), the removed member is
still granted the new generation and reads the rewrite — this test fails, which
is the bug.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SK = Path(__file__).resolve().parents[2] / "network" / "storagekit" / "tests"
sys.path.insert(0, str(_SK))

# Load the storagekit multi-member org harness under a unique name — a bare
# ``import conftest`` resolves to the repo-root conftest, not this one.
_spec = importlib.util.spec_from_file_location("sk_org_conftest", _SK / "conftest.py")
_sk = importlib.util.module_from_spec(_spec)
sys.modules["sk_org_conftest"] = _sk
_spec.loader.exec_module(_sk)
World = _sk.World  # noqa: E402

from tools.graph.schemas.registry import (  # noqa: E402
    SettingSchema, field, home, keyed_per_entity, publication_band, vaulted,
)
from tools.network.storagekit.keycontrol import KeyControlStore  # noqa: E402
from tools.vault.key_holder import VaultKeyCache, build_key_holder, _scoped_db  # noqa: E402
from tools.vault.key_sealer import build_vault_sealer  # noqa: E402
from tools.vault.db_content_store import DbContentStore  # noqa: E402
from tools.vault.storage_object import open_revision  # noqa: E402
from tools.vault.unlock import open_generation_keys  # noqa: E402


TEST_SET = "test.vault.org.audited"
ORG = "acme"


@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="setting_name")
@vaulted("audited")
class _OrgVaultedTestSet(SettingSchema):
    """A test-only org-homed vaulted set, so the sealer routes to an org store."""

    set_id = TEST_SET
    schema_revision = 1
    value: str = field(required=True, description="the secret")


def _ledger_provider(world):
    def provider(_set_id, _org):
        return (
            world.fold(),
            lambda heads: world.fold(heads=list(heads)),
            world.ancestry,
        )
    return provider


def _member_reads(locator, kem_private):
    """What a member holding *kem_private* can read at *locator*, or None.

    Exactly the login-recovery path: open every grant addressed to this
    member's credential into generation keys, then read the revision. A member
    with no grant for the revision's generation recovers nothing for it and
    cannot open — which is what removal must produce."""
    with KeyControlStore(_scoped_db(TEST_SET, ORG)) as kc:
        gens = open_generation_keys(kem_private, kc.accepted_grants(), kc.states)
    cache = VaultKeyCache()
    for state_id, generation_key in gens.items():
        cache.add(state_id, generation_key)
    control = build_key_holder(cache)(set_id=TEST_SET, org=ORG)
    try:
        return open_revision(
            locator, holdings=control.holdings, content_store=control.content_store
        )
    except Exception:
        return None


def _provision(world, grantor, newcomer_credential, cache, grantor_kem_state):
    """Expansion: grant a NEWLY-INVITED member the current generation head.

    The crib's expansion path — "one head capability grant, no state advance"
    — so a member who joined AFTER a secret was vaulted can read it. The
    grantor holds the current generation's secret in the sealer cache; this
    seals it to the newcomer's credential and persists the grant."""
    from tools.network.storagekit import distribution

    with KeyControlStore(_scoped_db(TEST_SET, ORG)) as kc:
        descriptor = kc.states[grantor_kem_state]
    grant = distribution.grant_current_head(
        grantor, descriptor.domain_id, newcomer_credential, descriptor,
        cache.secrets[grantor_kem_state], world.frontier(),
    )
    with KeyControlStore(_scoped_db(TEST_SET, ORG)) as kc:
        kc.accept_grant(grant)


def test_a_kicked_out_member_cannot_read_a_rewritten_org_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)

    # 1. Found the org with ONE member, Alice.
    world = World(member_count=1)
    alice = world.member(0)
    alice_kem = world.principals[alice.public_hex]["kem_private"]
    with KeyControlStore(_scoped_db(TEST_SET, ORG)) as kc:
        kc.accept_credential(world.principals[alice.public_hex]["credential"])

    cache = VaultKeyCache()
    seal = build_vault_sealer(cache, lambda: alice, _ledger_provider(world))

    # 2. Vault a secret BEFORE the second member exists; 3. Alice reads it.
    loc1 = seal(
        set_id=TEST_SET, schema_revision=1, key="github.token",
        setting_id="s-1", payload={"value": "secret-one"}, tier="audited", org=ORG,
    )
    assert _member_reads(loc1, alice_kem) == {"value": "secret-one"}
    g1_state = next(iter(cache.secrets))  # the current generation Alice minted

    # 4. NOW invite Bob, after the secret already exists. Enroll + provision him
    #    the current head (expansion).
    bob = world.admit(seed_index=40)
    bob_kem = world.principals[bob.public_hex]["kem_private"]
    with KeyControlStore(_scoped_db(TEST_SET, ORG)) as kc:
        kc.accept_credential(world.principals[bob.public_hex]["credential"])
    _provision(world, alice, world.principals[bob.public_hex]["credential"], cache, g1_state)

    # 5. Bob — who joined AFTER it was vaulted — can read the existing secret.
    assert _member_reads(loc1, bob_kem) == {"value": "secret-one"}, (
        "a member invited after the secret was vaulted should read it"
    )

    # 6. Kick Bob out (a frontier-advancing org contraction).
    world.remove(bob)

    # 7. Rewrite the secret — mints a NEW generation covering the removal.
    loc2 = seal(
        set_id=TEST_SET, schema_revision=1, key="github.token",
        setting_id="s-2", payload={"value": "secret-two"}, tier="audited", org=ORG,
    )

    # 8. Only Alice reads the rewrite. Bob was removed before it was written.
    assert _member_reads(loc2, alice_kem) == {"value": "secret-two"}, (
        "the remaining member must still read the rewrite"
    )
    assert _member_reads(loc2, bob_kem) != {"value": "secret-two"}, (
        "BUG (R6 violated): the kicked-out member read the rewritten secret — "
        "the new generation was granted to their stored credential instead of "
        "only to current members"
    )
