"""Unit tests for the Namecheap read-modify-write safety gates.

These prove offline exactly the failure modes the DEFINITION OF DONE calls out
for a second-model review: destructive record replacement, DKIM '+' corruption,
and false "one add" verification. No network is touched.
"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import namecheap_dns as nc  # noqa: E402

NS = "http://api.namecheap.com/xml.response"

# A realistic getHosts response: the six-record live set from the 2026-07-18
# run (mail A, tickets A, @ MX, @ SPF, DKIM with '+' and '/' in the key, DMARC),
# plus registry A added later. The DKIM value carries the dangerous characters.
DKIM_VALUE = "v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GN+ABC/def+ghi=="

LIVE_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<ApiResponse Status="OK" xmlns="{NS}">
  <Errors />
  <CommandResponse Type="namecheap.domains.dns.getHosts">
    <DomainDNSGetHostsResult Domain="auto.network" IsUsingOurDNS="true">
      <host HostId="1" Name="mail" Type="A" Address="5.161.179.179" MXPref="10" TTL="1800" />
      <host HostId="2" Name="tickets" Type="A" Address="5.161.179.179" MXPref="10" TTL="1800" />
      <host HostId="3" Name="@" Type="MX" Address="mail.auto.network." MXPref="10" TTL="1800" />
      <host HostId="4" Name="@" Type="TXT" Address="v=spf1 mx ~all" MXPref="10" TTL="1800" />
      <host HostId="5" Name="s20250112234._domainkey" Type="TXT" Address="{DKIM_VALUE}" MXPref="10" TTL="1800" />
      <host HostId="6" Name="_dmarc" Type="TXT" Address="v=DMARC1; p=quarantine; rua=mailto:dmarc@auto.network" MXPref="10" TTL="1800" />
      <host HostId="7" Name="registry" Type="A" Address="5.161.219.195" MXPref="10" TTL="1800" />
    </DomainDNSGetHostsResult>
  </CommandResponse>
</ApiResponse>"""

APEX = nc.Record(name="@", type="A", address="5.161.219.195", ttl="1800")
CREDS = nc.Credentials(api_user="u", api_key="k", client_ip="5.161.179.179", user_name="u")


def live_records():
    return nc.parse_hosts(LIVE_XML)


def test_parse_hosts_reads_all_records():
    recs = live_records()
    assert len(recs) == 7
    names = {r.name for r in recs}
    assert {"mail", "tickets", "@", "s20250112234._domainkey", "_dmarc", "registry"} <= names


def test_parse_hosts_rejects_non_ok_status():
    bad = LIVE_XML.replace('Status="OK"', 'Status="ERROR"')
    with pytest.raises(nc.DnsError):
        nc.parse_hosts(bad)


def test_mxpref_only_on_mx():
    recs = live_records()
    for r in recs:
        if r.type == "MX":
            assert r.mxpref == "10"
        else:
            assert r.mxpref is None


def test_critical_gate_passes_on_full_set():
    c = nc.check_critical(live_records())
    assert c.all_present, c.missing()


@pytest.mark.parametrize("drop_name,drop_type", [
    ("mail", "A"),        # mail A
    ("@", "MX"),          # MX
    ("s20250112234._domainkey", "TXT"),  # DKIM
])
def test_critical_gate_fails_when_a_record_is_missing(drop_name, drop_type):
    recs = [r for r in live_records() if not (r.name == drop_name and r.type == drop_type)]
    with pytest.raises(nc.DnsError):
        nc.assert_critical_present(recs)


def test_spf_and_dmarc_detected_by_shape():
    # Even if names differ, SPF/DMARC are recognised by their value shape.
    recs = [
        nc.Record("mail", "A", "5.161.179.179", "1800"),
        nc.Record("@", "MX", "mail.auto.network.", "1800", mxpref="10"),
        nc.Record("@", "TXT", "v=spf1 mx ~all", "1800"),
        nc.Record("_dmarc", "TXT", "v=DMARC1; p=none", "1800"),
        nc.Record("sel._domainkey", "TXT", "v=DKIM1; p=abc", "1800"),
    ]
    assert nc.check_critical(recs).all_present


def test_add_record_appends_exactly_one():
    before = live_records()
    after, added = nc.add_record(before, APEX)
    assert added is True
    assert len(after) == len(before) + 1
    d = nc.diff_records(before, after)
    assert d.clean_single_add
    assert d.added[0].key() == APEX.key()


def test_add_record_is_idempotent():
    before = live_records()
    once, _ = nc.add_record(before, APEX)
    twice, added = nc.add_record(once, APEX)
    assert added is False
    assert len(twice) == len(once)


def test_add_record_refuses_conflicting_address():
    before = [*live_records(), nc.Record("@", "A", "9.9.9.9", "1800")]
    with pytest.raises(nc.DnsError):
        nc.add_record(before, APEX)


def test_sethosts_body_encodes_dkim_plus_as_percent_2b():
    before = live_records()
    after, _ = nc.add_record(before, APEX)
    params = nc.build_sethosts_params("auto", "network", after, CREDS)
    body = nc.encode_body(params)
    # The '+' chars from the DKIM key must appear as %2B, never as a bare '+'
    # (a bare '+' decodes to a space and corrupts the key).
    assert "%2B" in body
    # Decode the body and confirm the DKIM address round-trips byte-identical.
    decoded = urllib.parse.parse_qs(body, keep_blank_values=True)
    dkim_addr = None
    for k, v in decoded.items():
        if v and v[0] == DKIM_VALUE:
            dkim_addr = v[0]
    assert dkim_addr == DKIM_VALUE, "DKIM value did not survive encode/decode intact"
    # And the safety assertion itself must pass on this body.
    nc.assert_dkim_safe(after, body)


def test_assert_dkim_safe_catches_unencoded_body():
    # The real corruption: a hand-written / unencoded body ships the DKIM '+'
    # literally, and Namecheap decodes it to a space. Simulate that raw body.
    before = live_records()
    after, _ = nc.add_record(before, APEX)
    raw = "&".join(
        f"{k}={v}" for k, v in nc.build_sethosts_params("auto", "network", after, CREDS)
    )
    assert "+" in raw  # the literal DKIM '+' survived unencoded
    assert "%2B" not in raw
    with pytest.raises(nc.DnsError):
        nc.assert_dkim_safe(after, raw)


def test_proper_urlencode_is_accepted_by_dkim_guard():
    # quote_plus (our encoder) turns '+' into %2B, so the guard passes.
    before = live_records()
    after, _ = nc.add_record(before, APEX)
    body = nc.encode_body(nc.build_sethosts_params("auto", "network", after, CREDS))
    nc.assert_dkim_safe(after, body)  # must not raise


def test_mxpref_emitted_only_for_mx_in_body():
    before = live_records()
    after, _ = nc.add_record(before, APEX)
    params = nc.build_sethosts_params("auto", "network", after, CREDS)
    keys = [k for k, _ in params]
    mx_indexes = [i for i, r in enumerate(after, start=1) if r.type == "MX"]
    a_indexes = [i for i, r in enumerate(after, start=1) if r.type == "A"]
    for i in mx_indexes:
        assert f"MXPref{i}" in keys
    for i in a_indexes:
        assert f"MXPref{i}" not in keys


def test_assert_clean_add_detects_removal():
    before = live_records()
    # After-set that DROPPED the DKIM record while adding the apex — the
    # catastrophic destructive-write case.
    after = [r for r in before if "_domainkey" not in r.name]
    after.append(APEX)
    with pytest.raises(nc.DnsError, match="DESTRUCTIVE"):
        nc.assert_clean_add(before, after, APEX)


def test_assert_clean_add_detects_extra_addition():
    before = live_records()
    after = [*before, APEX, nc.Record("evil", "A", "1.2.3.4", "1800")]
    with pytest.raises(nc.DnsError, match="exactly one"):
        nc.assert_clean_add(before, after, APEX)


def test_assert_clean_add_detects_wrong_value():
    before = live_records()
    wrong = nc.Record(name="@", type="A", address="9.9.9.9", ttl="1800")
    after = [*before, wrong]
    with pytest.raises(nc.DnsError, match="does not match"):
        nc.assert_clean_add(before, after, APEX)


def test_assert_clean_add_accepts_the_good_case():
    before = live_records()
    after = [*before, APEX]
    d = nc.assert_clean_add(before, after, APEX)
    assert d.clean_single_add


def test_every_prior_record_byte_identical_after_add():
    before = live_records()
    after, _ = nc.add_record(before, APEX)
    before_keys = {r.key() for r in before}
    after_keys = {r.key() for r in after}
    # Every prior record key is still present unchanged.
    assert before_keys <= after_keys
    # Exactly one new key.
    assert len(after_keys - before_keys) == 1


def test_load_credentials_parses_env(tmp_path):
    env = tmp_path / "namecheap-api.env"
    env.write_text(
        "# comment\n"
        'NAMECHEAP_API_USER="myuser"\n'
        "export NAMECHEAP_API_KEY=secretkey\n"
        "NAMECHEAP_CLIENT_IP=5.161.179.179\n"
    )
    creds = nc.load_credentials(str(env))
    assert creds.api_user == "myuser"
    assert creds.api_key == "secretkey"
    assert creds.client_ip == "5.161.179.179"
    assert creds.user_name == "myuser"  # defaults to api_user


def test_load_credentials_missing_file_is_dnserror(tmp_path):
    with pytest.raises(nc.DnsError, match="auto-ash-1"):
        nc.load_credentials(str(tmp_path / "nope.env"))
