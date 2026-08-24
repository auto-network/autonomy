"""Organization storage-domain sealer — the seam ``settings_ops`` uses there.

The write-direction twin of :mod:`tools.vault.key_holder`. ``settings_ops``
asks a registered *sealer* for the locator a ``@vaulted`` row stores in place
of its payload; in tests that sealer is injected, and in production nothing
built one, so every vaulted write failed closed with ``VaultSealerMissing``
and no vault row could ever be CREATED — the exact mirror of the read-side gap
``key_holder`` closed (``auto-a1pub``).

Personal secured Settings deliberately do not enter this module. They seal
directly to their owner policy class through ``personal_object`` and therefore
remain cold-writable without an organization delegate or fold. Organization
objects still fail closed here: there is no path that writes plaintext because
storage-domain sealing was unavailable.

## Three things this needs that the read side did not

Opening never mints. Sealing does — the first write after an access removal
advances the generation — so a sealer needs the material to author that
advance, which a holder never touches:

* an **author**, to sign,
* a **frontier**, the org's folded ledger, for its ``genesis_id``,
* an **ancestry** provider over the authority ledger.

## The author is the DELEGATE, never the persona

Crib ``1e005d5c-c11`` §12: the dashboard may hold domain content keys and the
agent delegate's signing key, and MUST NOT hold a persona signing key. So the
author here is the attenuated agent delegate (``auto-pw9bs.2``) whose chain
resolves to the member persona — not the persona itself.

That is not a style preference. Authoring as the persona would require a key
this process is forbidden to hold, so it would fail closed; the delegate is
what makes an unattended seal legitimate rather than a hole. The delegate is
scope-attenuated to exactly the two storage scopes and TTL-bounded, and
over-reach is refused by the FOLD rather than by an in-process check.

## Cold until a human unlocks, and that is the design

The delegate is MEMORY-class: empty after reboot until an unlock
re-provisions it. Crib §10 — "a reboot requiring a human to sign in and
reactivate the node is the ACCEPTED cost, not a defect to engineer around.
There is NO mode where a machine resumes usable without an unlock." So a
sealer registered before the first unlock has no author and refuses, and that
refusal is the system working.

Unattended operation on a fresh node therefore comes from the AGENT
performing the unlock — a throwaway persona whose factor the agents hold — and
never from the vault being warm across a restart. Do not add a provisioning
path that bypasses unlock; that is the hot-key-surviving-restart shape §10
retired.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tools.graph import settings_ops
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.credentials import (
    domain_member_keys,
    select_current_credential,
)
from tools.vault.policy_class import enable_public_sealing
from tools.vault.storage_object import Holdings, seal_revision
from tools.vault.store import VaultStore


def _current_member_credentials(frontier, key_control, authority_ancestry) -> tuple:
    """The grant recipients a mint seals to — crib §line-167.

    ``∀ p ∈ domain principals: seal(G_new, p.current_credential)``. The
    recipients are the CURRENT domain members read from the fold
    (``domain_member_keys``), each resolved to its ONE current credential
    (``select_current_credential``) — not every credential the store has ever
    held. A removed member is absent from the fold and receives nothing, which
    is how revocation (R6) holds; a superseded credential loses the tie-break,
    which is how a KEM rotation holds. Granting to ``accepted_credentials()``
    instead re-grants the new generation to removed and rotated-out keys — the
    crib's F-001 warns that "the re-key achieves NOTHING."
    """
    recipients = []
    for persona in sorted(domain_member_keys(frontier)):
        candidates = key_control.credentials_for_persona(persona)
        if not candidates:
            continue  # a current member who has not published a credential yet
        recipients.append(select_current_credential(candidates, authority_ancestry))
    return tuple(recipients)


class VaultSealerNotReady(RuntimeError):
    """No author is available, so nothing can be sealed.

    Raised rather than returned so the write aborts with the plaintext
    unwritten — ``_seal_vault_payload`` treats a raising sealer exactly like a
    missing one, which is what we want: the only outcomes are a real locator
    or no row.
    """



def build_vault_sealer(
    cache,
    author_provider: Callable[[], object],
    ledger_provider: Callable[[str, "str | None"], object],
) -> Callable[..., str]:
    """Return the sealer callable ``settings_ops`` invokes on a vaulted write.

    There are no store paths. A vault set's ciphertext and key-control records
    land in the SAME database file ``settings_ops`` writes the row to, resolved
    per call by :func:`~tools.vault.db_content_store.vault_db_path_for`. A
    single path here would have put every scope's secrets in one sidecar —
    both a store the design does not have, and a routing leak between
    organizations.

    ``cache`` is the same :class:`~tools.vault.key_holder.VaultKeyCache` the
    holder reads. The ``Holdings`` built here is deliberately byte-identical to
    the holder's: one shape, constructed the same way in both directions, so a
    value sealed by this process is opened by the same material that sealed it
    and a divergence cannot hide in a second construction.

    ``author_provider`` returns the attenuated agent delegate's SIGNING KEY,
    or ``None`` before an unlock has provisioned one — not the
    ``StorageDelegate`` wrapper, which carries the public facts around it.
    ``seal_revision`` signs with what it is given (``creator.sign_hex``), and
    the public half is what the fold resolves up the delegation chain to the
    member persona.

    A callable rather than a value because the delegate is renewed and revoked
    over a process's life, and a captured one would go stale exactly when it
    matters.

    ``ledger_provider(set_id, org)`` returns ``(frontier, fold_at,
    authority_ancestry)`` or ``None``. It takes the SET as well as the org
    because which fold to seal against is decided by the set's declared HOME,
    not by the org a caller happens to be acting as: a personal-homed set seals
    against the operator's own fold whatever org is in scope. Three seams rather than one, because they are not
    interchangeable and conflating them is silently wrong:

    * ``frontier`` is a folded VALUE, for :func:`seal_revision`.
    * ``fold_at`` is a CALLABLE — ``accept_state`` folds at the descriptor's
      own cited ``authority_heads``, not at ours.
    * ``authority_ancestry`` is the LEDGER's ancestry, and BOTH calls take it.
      Neither takes the storage DAG. The rule is the argument, not the callee:
      both seams resolve LOSS HEADS, and a loss head is a ledger event, so the
      closure has to be over ledger ids. ``keycontrol`` says it outright —
      acceptance runs with the ledger ancestry, "never this store's, which
      closes over storage ``state_id``s and is a different DAG over a different
      identifier space". The storage DAG is internal to the store's own
      reachability and is not an argument to anything here.

    A provider rather than captured values for the same reason the author is:
    the fold advances, and sealing against a stale frontier mints into a
    generation the org has moved past.
    """
    def sealer(
        *,
        set_id: str,
        schema_revision: int,
        key: str,
        setting_id: str,
        payload: dict,
        tier: str,
        org: "str | None",
        policy_class_id: "str | None" = None,
    ) -> str:
        author = author_provider()
        if author is None:
            raise VaultSealerNotReady(
                f"{set_id} is a vault set, but this process holds no agent "
                f"delegate to author the seal. The delegate is provisioned at "
                f"unlock and does not survive a restart, so unlock this node "
                f"before writing. This is storage authorship, not a vault "
                f"factor gesture or per-write operator approval."
            )
        ledger = ledger_provider(set_id, org)
        if ledger is None:
            raise VaultSealerNotReady(
                f"{set_id} is a vault set, but organization {org!r} has no "
                f"folded ledger to seal against — a secret is addressed by its "
                f"genesis, so an unfounded organization cannot hold one."
            )
        frontier, fold_at, authority_ancestry = ledger
        from tools.vault.key_holder import _scoped_db

        policy_class = None
        if tier == "secured":
            if not isinstance(policy_class_id, str) or not policy_class_id:
                raise VaultSealerNotReady(
                    "a secured Setting write must name the policy class whose "
                    "public key receives it"
                )
            from tools.graph.schemas.vault_policy_class import (
                VAULT_POLICY_CLASS_SET_ID,
            )

            class_db = _scoped_db(VAULT_POLICY_CLASS_SET_ID, org)
            with VaultStore(class_db) as class_store:
                policy_class = class_store.get_class(policy_class_id)
                if policy_class.current().sealing_public_key is None:
                    policy_class = enable_public_sealing(
                        policy_class,
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    class_store.put_class(policy_class)
        # Routed by the set's declared HOME, exactly as the ledger is: a
        # personal-homed set's ciphertext belongs in personal.db whatever org
        # the caller is acting as.
        from tools.vault.db_content_store import DbContentStore

        scoped = _scoped_db(set_id, org)
        with KeyControlStore(scoped) as key_control:
            holdings = Holdings(
                secrets=cache.secrets,
                descriptors=key_control.states,
                bridges=list(key_control.accepted_bridges()),
            )
            sealed = seal_revision(
                author=author,
                frontier=frontier,
                set_id=set_id,
                key=key,
                setting_id=setting_id,
                payload=payload,
                holdings=holdings,
                # AUTHORITY-ledger ancestry, the same one accept_state takes.
                # Both resolve LOSS HEADS, which are ledger events: this one
                # reaches state_covers(descriptor, frontier.loss_heads,
                # ancestry) -> ancestry(descriptor.covered_loss_heads). The
                # storage DAG is a different identifier space and appears in
                # neither top-level call — it is internal to the key-control
                # store's own reachability.
                ancestry=authority_ancestry,
                content_store=DbContentStore(scoped),
                tier=tier,
                policy_class=policy_class,
                # Every credential published in this store receives a grant
                # when this write mints a generation — the durable recovery
                # copy _land_advance persists. Without this, advance.grants
                # is () and grant persistence keeps nothing: the generation
                # dies with the process cache (graph://991c3b85-06e).
                recipient_credentials=_current_member_credentials(
                    frontier, key_control, authority_ancestry
                ),
            )
            if sealed.advance is not None:
                _land_advance(
                    sealed.advance, cache, key_control, fold_at,
                    authority_ancestry,
                )
            return sealed.locator

    return sealer


def _land_advance(advance, cache, key_control, fold_at, authority_ancestry) -> None:
    """Persist and cache the generation a write just minted.

    Sealing MINTS on the first vault write and on the first write after an
    access removal, and ``seal_revision`` deliberately does not publish what it
    minted: "its descriptor, bridges and grants are the caller's to hand to the
    broker." Dropping it does not fail the write — it writes an object sealed
    under a generation whose descriptor is in no store and whose secret is in
    no cache, so the row is written and CANNOT BE READ BACK, and the next write
    re-mints because it cannot find the first.

    Three things are load-bearing:

    * The DESCRIPTOR is accepted into the key-control store, so the holder —
      which builds its ``Holdings`` from exactly that store — finds the
      generation on the way back.
    * The SECRET is fed to the cache. ``seal_revision`` did add it to the
      holdings we passed, but ``VaultKeyCache.secrets`` hands out a COPY, so
      that mutation lands on a dict that dies with this call.
    * The GRANTS are persisted into the key-control store. Each seals the new
      generation's secret to a member's KEM credential; for a personal store —
      one member, the operator — that is a self-grant. It is the ONLY DURABLE
      copy of the generation secret: the cache is in-memory and dies on
      restart, so without the persisted grant a reboot loses the key and the
      secret can never be reopened. At the next unlock the operator re-derives
      their KEM key from the root and opens the grant to rebuild the cache
      (``open_generation_keys``). Grants for OTHER fleet members still reach
      them through the broker (``auto-pw9bs.3``); persisting here — the local
      recipient's own recovery copy — does not replace that.
    """
    key_control.accept_state(
        advance.descriptor,
        fold_at,
        authority_ancestry,
        bridges=tuple(advance.bridges),
    )
    cache.add(advance.descriptor.state_id, advance.secret)
    for grant in advance.grants:
        key_control.accept_grant(grant)


def register_vault_sealer(
    cache,
    author_provider: Callable[[], object],
    ledger_provider: Callable[[str, "str | None"], object],
) -> Callable[..., str]:
    """Build the sealer and install it as the process's vault sealer.

    Call once per process, alongside
    :func:`~tools.vault.key_holder.register_key_holder`. Registering before the
    first unlock is correct and intended: the providers are read live, so a
    write attempted before unlock fails with "no delegate to author the seal"
    — which says what to do — rather than with "no sealer registered", which
    reads like a missing installation.
    """
    sealer = build_vault_sealer(cache, author_provider, ledger_provider)
    settings_ops.set_vault_sealer(sealer)
    return sealer
