from __future__ import annotations

import subprocess

import pytest

from tools.dashboard.commit_broker.assembly import (
    assemble_signed_commit,
    commit_object_sha,
    fold_gpgsig_block,
    serialize_unsigned_commit_payload,
    unfold_gpgsig_block,
)

T = "0" * 40  # a fixture tree oid
P1 = "1" * 40
P2 = "2" * 40
AUTH = b"Ada <ada@example.com> 1700000000 +0000"
COMM = b"Ada <ada@example.com> 1700000005 +0000"


def _git_hash_commit(payload: bytes) -> str:
    """Ground truth: git's own object id for these exact bytes."""
    out = subprocess.run(
        ["git", "hash-object", "-t", "commit", "--stdin"],
        input=payload,
        capture_output=True,
        check=True,
    )
    return out.stdout.decode().strip()


# ── D5-1: byte-exact unsigned payload ────────────────────────────────

def test_serialize_matches_hand_built_golden_bytes():
    msg = "líne one\nsecond line\n".encode("utf-8")  # unicode + trailing newline
    payload = serialize_unsigned_commit_payload(
        tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=msg
    )
    golden = (
        b"tree " + T.encode() + b"\n"
        + b"parent " + P1.encode() + b"\n"
        + b"author " + AUTH + b"\n"
        + b"committer " + COMM + b"\n"
        + b"\n"
        + msg
    )
    assert payload == golden


def test_message_region_is_byte_identical_to_input():
    msg = "Åuthor bytes 世界\n\nbody\n".encode("utf-8")
    payload = serialize_unsigned_commit_payload(
        tree_oid=T, parent_oids=[], author_line=AUTH, committer_line=COMM, message=msg
    )
    # message is the tail after the single blank-line separator
    assert payload.endswith(msg)
    assert payload[payload.index(b"\n\n") + 2:] == msg
    assert b"gpgsig" not in payload


def test_parent_lines_preserve_order_first_parent_first():
    payload = serialize_unsigned_commit_payload(
        tree_oid=T, parent_oids=[P1, P2], author_line=AUTH, committer_line=COMM, message=b"m\n"
    )
    lines = payload.split(b"\n")
    assert lines[1] == b"parent " + P1.encode()
    assert lines[2] == b"parent " + P2.encode()


# ── D5-2: gpgsig folding ─────────────────────────────────────────────

ARMOR = (
    b"-----BEGIN PGP SIGNATURE-----\n"
    b"\n"  # interior blank line -> must become a single-space line
    b"iHUEABYKAB0WIQRexampleexampleexample\n"
    b"AAoJEexampleexampleexampleexample=\n"
    b"=abCd\n"
    b"-----END PGP SIGNATURE-----\n"
)


def test_fold_round_trips_back_to_the_original_armor():
    folded = fold_gpgsig_block(ARMOR)
    assert folded.startswith(b"gpgsig -----BEGIN PGP SIGNATURE-----")
    # every continuation begins with exactly one space
    for line in folded.split(b"\n")[1:]:
        assert line[:1] == b" "
    # interior blank line folded to a line containing exactly one space
    assert b"\n \n" in folded
    # unfold recovers the armor (minus the single trailing newline we dropped)
    assert unfold_gpgsig_block(folded) == ARMOR.rstrip(b"\n")


def test_fold_rejects_continuation_missing_its_leading_space():
    folded = fold_gpgsig_block(ARMOR)
    broken = folded.replace(b"\n iHUEA", b"\niHUEA", 1)  # strip a continuation's space
    with pytest.raises(ValueError):
        unfold_gpgsig_block(broken)


# ── D5-3: signed assembly + SHA recompute ────────────────────────────

def test_assembly_inserts_gpgsig_between_committer_and_blank_line():
    unsigned = serialize_unsigned_commit_payload(
        tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=b"subject\n"
    )
    folded = fold_gpgsig_block(ARMOR)
    signed, signed_sha = assemble_signed_commit(unsigned, folded)
    committer_line = b"committer " + COMM + b"\n"
    assert committer_line + folded + b"\n\n" in signed
    # message region unchanged from the unsigned payload
    assert signed[signed.index(b"\n\n") + 2:] == unsigned[unsigned.index(b"\n\n") + 2:]
    # signature insertion changes the id
    assert signed_sha != commit_object_sha(unsigned)
    assert signed_sha == _git_hash_commit(signed)


# ── D5-4: GA1 byte-identity gate across the fixture matrix ───────────

def _fixture_payloads():
    m_multi = b"subject line\n\nbody paragraph one\nbody paragraph two\n"
    m_signoff = b"fix thing\n\nSigned-off-by: Ada <ada@example.com>\n"
    m_unicode_id = b"\xc3\x85da \xe4\xb8\x96 <ada@example.com> 1700000000 +0000"
    return {
        "author_eq_committer": dict(tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=AUTH, message=b"m\n"),
        "author_ne_committer": dict(tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=b"m\n"),
        "zero_parent": dict(tree_oid=T, parent_oids=[], author_line=AUTH, committer_line=COMM, message=b"root\n"),
        "one_parent": dict(tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=b"m\n"),
        "two_parent_merge": dict(tree_oid=T, parent_oids=[P1, P2], author_line=AUTH, committer_line=COMM, message=b"merge\n"),
        "single_line_msg": dict(tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=b"one line\n"),
        "multi_line_msg": dict(tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=m_multi),
        "signoff_trailer": dict(tree_oid=T, parent_oids=[P1], author_line=AUTH, committer_line=COMM, message=m_signoff),
        "unicode_identity": dict(tree_oid=T, parent_oids=[P1], author_line=m_unicode_id, committer_line=m_unicode_id, message=b"m\n"),
    }


@pytest.mark.parametrize("label,fields", list(_fixture_payloads().items()))
def test_ga1_unsigned_sha_matches_git_hash_object(label, fields):
    payload = serialize_unsigned_commit_payload(**fields)
    assert commit_object_sha(payload) == _git_hash_commit(payload), label


@pytest.mark.parametrize("label,fields", list(_fixture_payloads().items()))
def test_ga1_signed_sha_matches_git_hash_object(label, fields):
    signed, signed_sha = assemble_signed_commit(
        serialize_unsigned_commit_payload(**fields), fold_gpgsig_block(ARMOR)
    )
    assert signed_sha == _git_hash_commit(signed), label


def test_ga1_gate_is_not_a_rubber_stamp_a_trimmed_newline_flips_the_id():
    fields = _fixture_payloads()["multi_line_msg"]
    good = serialize_unsigned_commit_payload(**fields)
    corrupted = good[:-1]  # drop the message's trailing newline
    # a single-byte corruption must change the git id — the gate has teeth
    assert commit_object_sha(corrupted) != commit_object_sha(good)
    assert _git_hash_commit(corrupted) != _git_hash_commit(good)
