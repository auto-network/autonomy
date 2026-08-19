"""Can a PERSONAL-homed vaulted set actually be written?

`autonomy.vault.audited` and `.secured` are declared `@home("personal")` and
`@vaulted(...)`. Nothing had exercised that pair — the sealer's own tests all
pass an org that happens to have a founded ledger behind it, so they prove the
ORG path and say nothing about the personal one.

The design distinguishes them sharply. An ORG secret's CEK is additionally
wrapped under storage-state secrets so every member can reach it, which needs
a genesis, a frontier and a generation. A PERSONAL secret's CEK is wrapped
under the owner's vault master KEK and stops there — "Personal object => N=1,
degenerate, no storage domain involved" (`graph://193fa89e-313` R4).

`seal_revision` implements only the first. These tests pin what that means for
a personal-homed set today, so the answer is a test result rather than an
argument.
"""

from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
)
from tools.vault.key_holder import VaultKeyCache
from tools.vault.key_sealer import VaultSealerNotReady, register_vault_sealer

import tools.graph.schemas  # noqa: F401 — registers the sets


@pytest.fixture(autouse=True)
def _no_sealer():
    settings_ops.set_vault_sealer(None)
    yield
    settings_ops.set_vault_sealer(None)


def test_the_set_is_declared_personal_and_vaulted():
    """The premise. If either declaration changes, the rest of this file is
    describing a set that no longer exists."""
    from tools.graph import schemas

    assert schemas.declared_home(VAULT_AUDITED_SET_ID) == "personal"
    assert schemas.declared_vault_tier(VAULT_AUDITED_SET_ID) == "audited"


def test_seal_revision_cannot_seal_without_an_organization():
    """THE ONE THAT MATTERS.

    The production sealer resolves an org's ledger to get a frontier, because
    `seal_revision` derives its content domain from `frontier.genesis_id`. The
    operator's own store is not an organization and has no genesis, so the
    provider has nothing to return and the write refuses.

    This is not a bug in the sealer — it is the sealer being honest that it
    implements the ORG path. It is a gap in the SET: a personal-homed vaulted
    row needs the owner-wrap path (CEK under the vault master KEK), and that
    path is not wired into settings.
    """
    register_vault_sealer(
        VaultKeyCache(),
        "/tmp/does-not-matter/kc.db",
        "/tmp/does-not-matter/content",
        lambda: object(),          # an author is available
        lambda org: None,          # ...but the personal store has no ledger
    )

    with pytest.raises(VaultSealerNotReady, match="no folded ledger"):
        settings_ops.add_setting(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
            "github.token", {"value": "ghp_" + "a" * 36}, org=None,
        )


def test_the_refusal_names_the_organization_rather_than_the_mechanism():
    """A caller hitting this must be able to tell it is a HOMING problem and
    not a missing installation, or the next person re-registers the sealer and
    gets the same refusal."""
    register_vault_sealer(
        VaultKeyCache(), "/tmp/x/kc.db", "/tmp/x/content",
        lambda: object(), lambda org: None,
    )

    with pytest.raises(VaultSealerNotReady) as excinfo:
        settings_ops.add_setting(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
            "github.token", {"value": "x" * 40}, org=None,
        )

    message = str(excinfo.value)
    assert "genesis" in message
    assert "organization" in message


@pytest.mark.xfail(
    strict=True,
    reason=(
        "auto-ehyoh's whole point: the operator's GitHub token lives in their "
        "OWN store, released unattended. The set is declared for it, but a "
        "personal object's CEK must be wrapped under the owner's vault master "
        "KEK and that path is not wired into settings — only the org "
        "storage-domain path is, and it needs a genesis the personal store "
        "does not have. Flips to xpass the day the owner-wrap lands, which is "
        "the signal that ehyoh can move."
    ),
)
def test_ehyoh_can_store_the_operators_github_token():
    """What the demo needs, expressed as the test that will one day pass.

    Written failing on purpose. The refusal tests above pin what the system
    does TODAY; this one pins what it must do, so the gap is a red mark
    somebody has to answer for rather than a paragraph in a note.
    """
    register_vault_sealer(
        VaultKeyCache(), "/tmp/x/kc.db", "/tmp/x/content",
        lambda: object(), lambda org: None,
    )

    settings_ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
        "github.token", {"value": "ghp_" + "a" * 36}, org=None,
    )
