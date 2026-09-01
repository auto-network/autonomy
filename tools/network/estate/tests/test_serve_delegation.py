"""auto-g1jxw: the parent-zone delegation diff — exactly four records,
rendered and verified before any write, mail records untouchable.
"""

from __future__ import annotations

import pytest

from tools.network.estate.namecheap_dns import (
    DnsError,
    Record,
    add_delegation_records,
    assert_clean_multi_add,
    check_critical,
    serve_delegation,
)

PRIMARY_IP = "203.0.113.10"
SECONDARY_IP = "198.51.100.20"


def _live_set() -> list[Record]:
    """A live-shaped parent set: apex A, mail records, registry names."""
    return [
        Record("@", "A", "5.161.219.195", "1800"),
        Record("mail", "A", "5.161.179.179", "1800"),
        Record("@", "MX", "mail.auto.network.", "1800", mxpref="10"),
        Record("default._domainkey", "TXT", "v=DKIM1; k=rsa; p=MIIB+B/w==", "1800"),
        Record("@", "TXT", "v=spf1 a mx ~all", "1800"),
        Record("_dmarc", "TXT", "v=DMARC1; p=quarantine", "1800"),
        Record("registry", "A", "5.161.219.195", "1800"),
        Record("relay", "A", "5.161.219.195", "1800"),
    ]


def test_delegation_is_exactly_four_pinned_records():
    records = serve_delegation(PRIMARY_IP, SECONDARY_IP)
    assert [(r.name, r.type, r.address) for r in records] == [
        ("serve", "NS", "ns1.serve.auto.network."),
        ("serve", "NS", "ns2.serve.auto.network."),
        ("ns1.serve", "A", PRIMARY_IP),
        ("ns2.serve", "A", SECONDARY_IP),
    ]
    assert all(r.ttl == "3600" for r in records)


def test_add_delegation_appends_four_and_preserves_critical():
    before = _live_set()
    after, added = add_delegation_records(
        before, serve_delegation(PRIMARY_IP, SECONDARY_IP)
    )
    assert len(added) == 4
    assert len(after) == len(before) + 4
    assert check_critical(after).all_present
    # The original records survive byte-identically.
    assert {r.key() for r in before} <= {r.key() for r in after}


def test_add_delegation_is_idempotent():
    once, _ = add_delegation_records(
        _live_set(), serve_delegation(PRIMARY_IP, SECONDARY_IP)
    )
    twice, added = add_delegation_records(
        once, serve_delegation(PRIMARY_IP, SECONDARY_IP)
    )
    assert added == []
    assert {r.key() for r in twice} == {r.key() for r in once}


def test_same_name_ns_pair_is_legitimate_but_glue_conflict_refused():
    # Two NS records for "serve" with different targets must coexist —
    # that IS a delegation. A glue A record pointing somewhere else is
    # the destructive case and fails closed.
    conflicted = _live_set() + [Record("ns1.serve", "A", "192.0.2.99", "3600")]
    with pytest.raises(DnsError, match="ns1.serve"):
        add_delegation_records(
            conflicted, serve_delegation(PRIMARY_IP, SECONDARY_IP)
        )


def test_clean_multi_add_catches_removal_extra_and_mismatch():
    before = _live_set()
    news = serve_delegation(PRIMARY_IP, SECONDARY_IP)
    good = before + news
    assert_clean_multi_add(before, good, news)

    with pytest.raises(DnsError, match="DESTRUCTIVE"):
        assert_clean_multi_add(before, good[1:], news)  # a removal
    with pytest.raises(DnsError):
        assert_clean_multi_add(
            before, good + [Record("x", "A", "192.0.2.1", "60")], news
        )  # an unexpected extra
    wrong = before + news[:3] + [Record("ns2.serve", "A", "192.0.2.7", "3600")]
    with pytest.raises(DnsError):
        assert_clean_multi_add(before, wrong, news)  # landed wrong value
