"""Per-link channel keys — mint, resolve, drop (graph://807b4e11-3e9).

Every content share link gets a fresh Ed25519 keypair at publish. The PUBLIC
key rides the link's URL fragment (presentation-side only — browsers never
send fragments, so no server ever sees it) and is cached on the grant row as
``channel_pub``. The PRIVATE seed is one org-vaulted, audited-tier settings
row (``autonomy.network.link-channel-key``) keyed by the same grant token:
org synchronization replicates it to members, any member session reads it
unattended, every read is on the audit record, and revocation deletes it.

Holding the seed is what authorizes an endpoint to serve the link; the
viewer verifies the serving handshake against the fragment key. Custody
narrows to a chartered subgroup (graph://fe4499fa-0e9) by re-sealing the
vault object — nothing here changes for that.

org:join and fleet:* links are OUT of scope: their fragments already carry
invitation bearer material and their viewers speak the join-bridge protocol,
not the share-link handshake.
"""

from __future__ import annotations

import base64
import logging

from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_CHANNEL_KEY_REVISION,
    NETWORK_LINK_CHANNEL_KEY_SET_ID,
)
from tools.network.idkit import KeyPair

logger = logging.getLogger("dashboard.link_channel_key")

#: Target types whose links carry a channel keypair. Everything content-like;
#: membership links keep their own fragment semantics.
CHANNEL_KEY_TARGET_TYPES = frozenset({"design", "file", "mission", "note", "present"})


class ChannelKeyUnavailable(Exception):
    """Minting or resolving a channel key failed; the message names why
    (typically: the vault is not warm). Operator-facing."""


def mint_channel_key(token: str, org: str | None) -> str:
    """Mint the link's keypair, vault the seed, return the public key hex.

    Fails closed: if the vaulted write cannot happen (no sealer — the vault
    is cold), nothing is stored and :class:`ChannelKeyUnavailable` is raised
    with the sealer's own message. The caller decides whether the publish
    survives without a channel key (it should not).
    """
    pair = KeyPair.generate()
    try:
        # A vaulted set is written with add_setting (it seals the payload and
        # stores the locator); the token is CSPRNG-unique, so a create never
        # collides. upsert_by_key is refused on vault sets by design.
        settings_ops.add_setting(
            NETWORK_LINK_CHANNEL_KEY_SET_ID,
            NETWORK_LINK_CHANNEL_KEY_REVISION,
            token,
            {"seed": pair.private_hex},
            org=org,
        )
    except Exception as exc:
        raise ChannelKeyUnavailable(
            f"could not vault the link's channel key: {exc}"
        ) from exc
    return pair.public_hex


def channel_key_for(token: str, org: str | None) -> KeyPair:
    """The link's signing keypair, resolved from the org vault.

    Unattended for an authorized member session (audited tier). Raises
    :class:`ChannelKeyUnavailable` naming the refusal when the row is
    absent (legacy or revoked link) or the vault read fails closed.
    """
    row = settings_ops.read_set_key(
        NETWORK_LINK_CHANNEL_KEY_SET_ID, token, org=org)
    if row is None:
        raise ChannelKeyUnavailable(
            "this link has no channel key (legacy or revoked) — re-mint it")
    vault_error = row.get("vault_error")
    if vault_error:
        raise ChannelKeyUnavailable(
            f"the link's channel key did not open: {vault_error}")
    payload = row.get("payload") or {}
    seed = payload.get("seed")
    if not isinstance(seed, str):
        raise ChannelKeyUnavailable(
            "the link's channel-key row opened without a seed")
    return KeyPair.from_private_hex(seed)


def drop_channel_key(token: str, org: str | None) -> bool:
    """Delete the link's vaulted seed row — revocation's key half. Best
    effort by design: the grant row is the serving gate; a surviving seed
    row for a dead grant serves nothing."""
    try:
        for m in settings_ops.read_owned_set(
            NETWORK_LINK_CHANNEL_KEY_SET_ID,
            org=org,
            target_revision=NETWORK_LINK_CHANNEL_KEY_REVISION,
        ).members:
            if m.key == token:
                settings_ops.remove_setting(m.id, org=org)
                return True
    except Exception:
        logger.warning("channel-key drop failed for token=%s", token[:8],
                       exc_info=True)
    return False


def fragment_url(url: str, channel_pub_hex: str) -> str:
    """The full shareable URL: canonical url + '#' + base64url(public key),
    unpadded (43 chars). The stored grant keeps the canonical url; the
    fragment exists only where a person copies the link."""
    raw = bytes.fromhex(channel_pub_hex)
    frag = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{url}#{frag}"


def pub_from_fragment(fragment: str) -> str:
    """Decode a fragment back to the 64-hex public key; raises ValueError
    on anything that is not exactly a 32-byte base64url value."""
    pad = "=" * (-len(fragment) % 4)
    raw = base64.urlsafe_b64decode(fragment + pad)
    if len(raw) != 32:
        raise ValueError("fragment is not a 32-byte channel key")
    return raw.hex()
