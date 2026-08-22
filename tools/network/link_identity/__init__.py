"""Shared identity-pin commitments for auto.network links.

The relay token routes a link; this package pins the stable identity that the
viewer must authenticate after opening that channel.  It is deliberately
application-neutral so Fleet, persona-bound, and organization-wide links use
one wire format.
"""

from .commitment import (  # noqa: F401
    COMMITMENT_DOMAIN,
    LinkBinding,
    LinkBindingKind,
    commitment_fragment,
    verify_fragment,
)
