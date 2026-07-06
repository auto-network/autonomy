"""Byte-exact canonical commit-object encoding and signed-object assembly (DN5 §1-2).

A git commit object's id is ``sha1(b"commit " + len + b"\\0" + payload)``. The
signature is computed over the *unsigned* payload; the final published object
inserts a ``gpgsig`` header between the ``committer`` line and the blank line,
then the id is recomputed over the signed bytes. Assembly never amends an
existing object — it produces new bytes and a new id.

Everything here operates on ``bytes`` so unicode names/emails and verbatim
message bytes pass through with no re-encoding. Timestamps and tz offsets are
read from the stored snapshot, never recomputed from a clock.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


def commit_object_sha(payload: bytes) -> str:
    """Return the git object id for a commit ``payload``.

    ``sha1(b"commit " + <byte length> + b"\\0" + payload)`` — the length is the
    byte length of ``payload`` (unsigned or signed, whichever is passed).
    """
    header = b"commit " + str(len(payload)).encode("ascii") + b"\0"
    return hashlib.sha1(header + payload).hexdigest()


def serialize_unsigned_commit_payload(
    *,
    tree_oid: str,
    parent_oids: Sequence[str],
    author_line: bytes,
    committer_line: bytes,
    message: bytes,
) -> bytes:
    """Emit the unsigned canonical commit payload, byte-exact (DN5 D5-1).

    Order: ``tree <oid>\\n``, one ``parent <oid>\\n`` per parent in commit order
    (``parent_oids[0]`` is the first-parent), ``author <line>\\n``,
    ``committer <line>\\n``, a single blank line ``\\n``, then ``message``
    verbatim (including its own trailing newline).

    ``author_line`` / ``committer_line`` are the identity+time bytes that follow
    the ``author ``/``committer `` keyword (``Name <email> <unix_ts> <tzoffset>``)
    and are emitted with no re-encoding, re-wrapping, or trimming. ``message`` is
    copied through byte-for-byte. No ``gpgsig`` line is present at this stage.
    """
    out = bytearray()
    out += b"tree " + tree_oid.encode("ascii") + b"\n"
    for parent in parent_oids:
        out += b"parent " + parent.encode("ascii") + b"\n"
    out += b"author " + author_line + b"\n"
    out += b"committer " + committer_line + b"\n"
    out += b"\n"
    out += message
    return bytes(out)


def fold_gpgsig_block(armor: bytes) -> bytes:
    """Fold an armored signature into a ``gpgsig`` header block (DN5 D5-2).

    The first armor line follows ``gpgsig `` (single space, no extra leading
    space); every continuation line is prefixed with exactly one space; a blank
    line inside the armor becomes a line containing exactly one space. This is
    git's RFC-4880-style header folding and is identical for GPG (OpenPGP) and
    SSH (``gpg.format=ssh``) armor bodies. A single trailing newline on ``armor``
    is not treated as a continuation line.
    """
    lines = armor.split(b"\n")
    # Drop a single trailing newline's empty tail so it is not folded into a
    # spurious trailing " " line; genuine interior blank lines are preserved.
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        return b"gpgsig "
    out = bytearray(b"gpgsig " + lines[0])
    for line in lines[1:]:
        out += b"\n " + line
    return bytes(out)


def unfold_gpgsig_block(folded: bytes) -> bytes:
    """Inverse of :func:`fold_gpgsig_block` — recover the original armor bytes.

    Strips the ``gpgsig `` prefix from the first line and exactly one leading
    space from each continuation line. Used by the round-trip proof.
    """
    lines = folded.split(b"\n")
    if not lines:
        return b""
    first = lines[0]
    if not first.startswith(b"gpgsig "):
        raise ValueError("folded gpgsig block does not start with 'gpgsig '")
    recovered = bytearray(first[len(b"gpgsig "):])
    for line in lines[1:]:
        if not line.startswith(b" "):
            raise ValueError("gpgsig continuation line is missing its leading space")
        recovered += b"\n" + line[1:]
    return bytes(recovered)


def assemble_signed_commit(unsigned_payload: bytes, folded_gpgsig: bytes) -> tuple[bytes, str]:
    """Insert the ``gpgsig`` block and recompute the signed id (DN5 D5-3).

    The block is placed immediately after the ``committer`` line and before the
    blank line separating headers from the message; the message region is left
    byte-identical to ``unsigned_payload``. Returns ``(signed_payload,
    signed_commit_sha)`` where the id is recomputed over the *signed* bytes, so
    it differs from the unsigned id.
    """
    sep = unsigned_payload.find(b"\n\n")
    if sep == -1:
        raise ValueError("unsigned payload has no header/message separator")
    headers = unsigned_payload[:sep]          # up to and including the committer line's text
    message = unsigned_payload[sep + 2:]      # after the blank line
    signed_payload = headers + b"\n" + folded_gpgsig + b"\n\n" + message
    return signed_payload, commit_object_sha(signed_payload)
