#!/usr/bin/env python3
"""Namecheap DNS read-modify-write for the auto.network estate.

``namecheap.domains.dns.setHosts`` is a **full replace**: it overwrites the
ENTIRE record set in one call. mail.auto.network's A/MX/SPF/DKIM/DMARC all
live in that set, so a naive setHosts that names only the new record DELETES
MAIL (proven-runbook pitfall, graph://bcf6328a-d3f). This module never lets
that happen: it always reads the live set, appends in code, refuses to write
unless the critical mail records survived the read, and re-reads to prove the
after-diff is exactly one addition and zero removals.

Two encoding facts are load-bearing and are why this is code, not curl:

  * The DKIM public key contains ``+`` and ``/``. In an x-www-form-urlencoded
    body a literal ``+`` decodes to a space, silently corrupting the key.
    ``urllib.parse.urlencode`` (quote_via=quote_plus, the default) emits ``+``
    as ``%2B`` and spaces as ``+``; Namecheap decodes both correctly. We
    assert this on the built body before any write.
  * Namecheap calls the host field ``Name`` in getHosts output but
    ``HostNameN`` in setHosts input, and ``MXPrefN`` is sent ONLY for MX
    records. Emitting MXPref on an A record is a silent corruption.

The pure functions (parse / gate / add / build / diff) take no network and
are unit-tested in tests/test_namecheap_dns.py. Network I/O is a thin shell
around them so the dangerous logic is provable offline.

The API's whitelisted client IP is auto-ash-1's own public IP, so the live
subcommands MUST run on auto-ash-1 (5.161.179.179); from anywhere else
Namecheap refuses the call. See graph://dca4002d-a8a.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

API_ENDPOINT = "https://api.namecheap.com/xml.response"
# Namecheap XML lives in this default namespace on every element.
_NS = "http://api.namecheap.com/xml.response"


class DnsError(Exception):
    """A safety gate refused, or the API returned an error."""


@dataclass(frozen=True)
class Record:
    """One DNS host record, normalised for order-independent comparison.

    ``mxpref`` is None for every non-MX type; it participates in equality so
    an MX priority flip is caught by the before/after diff.
    """

    name: str
    type: str
    address: str
    ttl: str
    mxpref: str | None = None

    def key(self) -> str:
        """Byte-stable identity used for set diffing and dedupe."""
        mx = "" if self.mxpref is None else self.mxpref
        return f"{self.name}\t{self.type}\t{self.address}\t{self.ttl}\t{mx}"


# ── parsing ──────────────────────────────────────────────────────────────

def _q(tag: str) -> str:
    return f"{{{_NS}}}{tag}"


def _api_status(root: ET.Element) -> str:
    return (root.get("Status") or "").upper()


def _api_errors(root: ET.Element) -> list[str]:
    errs = root.find(_q("Errors"))
    if errs is None:
        return []
    return [(e.text or "").strip() for e in errs.findall(_q("Error"))]


def parse_hosts(xml_text: str) -> list[Record]:
    """Parse a getHosts XML response into a list of Records.

    Raises DnsError if the API reported a non-OK status — a failed read must
    never be mistaken for "zero records" and drive a destructive write.
    """
    root = ET.fromstring(xml_text)
    if _api_status(root) != "OK":
        raise DnsError(
            "getHosts did not return Status=OK: "
            + "; ".join(_api_errors(root) or ["unknown error"])
        )
    result = root.find(f".//{_q('DomainDNSGetHostsResult')}")
    if result is None:
        raise DnsError("getHosts response has no DomainDNSGetHostsResult")
    records: list[Record] = []
    for host in result.findall(_q("host")):
        rtype = (host.get("Type") or "").upper()
        records.append(
            Record(
                name=host.get("Name") or "",
                type=rtype,
                address=host.get("Address") or "",
                ttl=host.get("TTL") or "1800",
                mxpref=host.get("MXPref") if rtype == "MX" else None,
            )
        )
    return records


# ── safety gate: the critical mail records must be present ───────────────

@dataclass
class CriticalCheck:
    mail_a: bool = False
    mx: bool = False
    spf: bool = False
    dmarc: bool = False
    dkim: bool = False

    @property
    def all_present(self) -> bool:
        return all(field_val for field_val in vars(self).values())

    def missing(self) -> list[str]:
        return [name for name, present in vars(self).items() if not present]


def check_critical(records: list[Record]) -> CriticalCheck:
    """Identify the mail records whose loss is catastrophic.

    Deliberately conservative: we look for the *shape* of each record, not an
    exact string, so a legitimate value change (e.g. a rotated DKIM selector)
    still satisfies the gate while an empty/short read fails it.
    """
    c = CriticalCheck()
    for r in records:
        name = r.name.lower()
        addr = r.address.lower()
        if r.type == "A" and name == "mail":
            c.mail_a = True
        if r.type == "MX":
            c.mx = True
        if r.type == "TXT" and "v=spf1" in addr:
            c.spf = True
        if r.type == "TXT" and (name == "_dmarc" or "v=dmarc1" in addr):
            c.dmarc = True
        if "_domainkey" in name or "v=dkim1" in addr:
            c.dkim = True
    return c


def assert_critical_present(records: list[Record]) -> CriticalCheck:
    c = check_critical(records)
    if not c.all_present:
        raise DnsError(
            "refusing to write: critical mail records missing from the live "
            f"read → {', '.join(c.missing())}. A destructive setHosts here "
            "would delete mail. Aborting before any mutation."
        )
    return c


# ── modify: append exactly one record, idempotently ──────────────────────

def add_record(records: list[Record], new: Record) -> tuple[list[Record], bool]:
    """Return (records_with_new, added?).

    Idempotent: if an identical record already exists, the set is returned
    unchanged and added=False. A same-name/type record with a *different*
    address is a conflict we refuse — silently replacing it would be exactly
    the destructive edit this module exists to prevent.
    """
    existing_keys = {r.key() for r in records}
    if new.key() in existing_keys:
        return list(records), False
    for r in records:
        if r.name == new.name and r.type == new.type and r.address != new.address:
            raise DnsError(
                f"refusing: a {new.type} record for {new.name!r} already "
                f"exists with a different address ({r.address!r} vs "
                f"{new.address!r}). Resolve by hand — this tool only appends."
            )
    return [*records, new], True


# ── build the setHosts request body ──────────────────────────────────────

def build_sethosts_params(
    sld: str,
    tld: str,
    records: list[Record],
    creds: "Credentials",
) -> list[tuple[str, str]]:
    """Positional HostNameN/RecordTypeN/AddressN/TTLN(/MXPrefN) params.

    Returned as an ordered list of pairs so encoding is explicit and testable.
    MXPref is emitted ONLY for MX records.
    """
    params: list[tuple[str, str]] = [
        ("ApiUser", creds.api_user),
        ("ApiKey", creds.api_key),
        ("UserName", creds.user_name),
        ("ClientIp", creds.client_ip),
        ("Command", "namecheap.domains.dns.setHosts"),
        ("SLD", sld),
        ("TLD", tld),
    ]
    for i, r in enumerate(records, start=1):
        params.append((f"HostName{i}", r.name))
        params.append((f"RecordType{i}", r.type))
        params.append((f"Address{i}", r.address))
        params.append((f"TTL{i}", r.ttl))
        if r.type == "MX":
            # MXPref is required for MX and must not appear on other types.
            params.append((f"MXPref{i}", r.mxpref or "10"))
    return params


def encode_body(params: list[tuple[str, str]]) -> str:
    """URL-encode the setHosts body with correct ``+`` handling.

    urlencode defaults to quote_via=quote_plus: ``+`` → ``%2B`` and space →
    ``+``. This is the single line that keeps the DKIM key byte-identical.
    """
    return urllib.parse.urlencode(params)


def assert_dkim_safe(records: list[Record], body: str) -> None:
    """Prove no DKIM ``+`` survived as a literal ``+`` in the encoded body.

    Any ``+`` in a record Address must appear as ``%2B``. A bare ``+`` in the
    body is only ever a legitimately-encoded space; a DKIM key has no spaces,
    so a mis-encoded ``+`` there would corrupt it. We assert the encoded form
    contains the ``%2B`` escape for every ``+`` present in the source values.
    """
    plus_in_values = sum(r.address.count("+") for r in records)
    if plus_in_values and body.count("%2B") < plus_in_values:
        raise DnsError(
            "encoding check failed: a '+' in a record value was not encoded "
            "as %2B — the DKIM key would be corrupted. Refusing to write."
        )


# ── verify: the after-diff must be exactly one addition ──────────────────

@dataclass
class Diff:
    added: list[Record] = field(default_factory=list)
    removed: list[Record] = field(default_factory=list)

    @property
    def clean_single_add(self) -> bool:
        return len(self.added) == 1 and len(self.removed) == 0


def diff_records(before: list[Record], after: list[Record]) -> Diff:
    """Set difference by byte-stable key: what appeared, what disappeared."""
    before_by_key = {r.key(): r for r in before}
    after_by_key = {r.key(): r for r in after}
    added = [r for k, r in after_by_key.items() if k not in before_by_key]
    removed = [r for k, r in before_by_key.items() if k not in after_by_key]
    return Diff(added=added, removed=removed)


def assert_clean_add(before: list[Record], after: list[Record], new: Record) -> Diff:
    """Fail unless after == before + exactly the one new record.

    Guards all three failure modes at once: a removal (mail loss), an
    unexpected extra addition, or the new record landing with the wrong value.
    """
    d = diff_records(before, after)
    if d.removed:
        raise DnsError(
            f"DESTRUCTIVE WRITE DETECTED: {len(d.removed)} record(s) removed: "
            + "; ".join(r.key() for r in d.removed)
        )
    if len(d.added) != 1:
        raise DnsError(
            f"expected exactly one addition, saw {len(d.added)}: "
            + "; ".join(r.key() for r in d.added)
        )
    if d.added[0].key() != new.key():
        raise DnsError(
            "the added record does not match the intended record.\n"
            f"  intended: {new.key()}\n  landed:   {d.added[0].key()}"
        )
    return d


# ── credentials + live I/O (thin) ────────────────────────────────────────

@dataclass
class Credentials:
    api_user: str
    api_key: str
    client_ip: str
    user_name: str


def load_credentials(env_path: str = "/etc/namecheap-api.env") -> Credentials:
    """Read /etc/namecheap-api.env (0600, on auto-ash-1) into Credentials.

    Format is shell `KEY=value` lines; we parse without sourcing so a stray
    command in the file can't execute. UserName defaults to the API user.
    """
    values: dict[str, str] = {}
    p = Path(env_path)
    if not p.is_file():
        raise DnsError(
            f"credentials file {env_path} not found. This subcommand must run "
            "on auto-ash-1 (5.161.179.179), whose IP is whitelisted with "
            "Namecheap; from anywhere else the API refuses the call."
        )
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    try:
        api_user = values["NAMECHEAP_API_USER"]
        return Credentials(
            api_user=api_user,
            api_key=values["NAMECHEAP_API_KEY"],
            client_ip=values["NAMECHEAP_CLIENT_IP"],
            user_name=values.get("NAMECHEAP_USER_NAME", api_user),
        )
    except KeyError as exc:
        raise DnsError(f"{env_path} missing required key: {exc.args[0]}") from exc


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed host)
        return resp.read().decode("utf-8")


def _http_post(url: str, body: str) -> str:
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed host)
        return resp.read().decode("utf-8")


def live_gethosts(creds: Credentials, sld: str, tld: str) -> str:
    """Return the raw getHosts XML (kept raw so it can be saved verbatim)."""
    params = urllib.parse.urlencode(
        [
            ("ApiUser", creds.api_user),
            ("ApiKey", creds.api_key),
            ("UserName", creds.user_name),
            ("ClientIp", creds.client_ip),
            ("Command", "namecheap.domains.dns.getHosts"),
            ("SLD", sld),
            ("TLD", tld),
        ]
    )
    return _http_get(f"{API_ENDPOINT}?{params}")


def live_sethosts(creds: Credentials, sld: str, tld: str, records: list[Record]) -> str:
    params = build_sethosts_params(sld, tld, records, creds)
    body = encode_body(params)
    assert_dkim_safe(records, body)
    xml = _http_post(API_ENDPOINT, body)
    root = ET.fromstring(xml)
    if _api_status(root) != "OK":
        raise DnsError(
            "setHosts did not return Status=OK: "
            + "; ".join(_api_errors(root) or ["unknown error"])
        )
    return xml


# ── CLI ──────────────────────────────────────────────────────────────────

def _fmt(records: list[Record]) -> str:
    return "\n".join(sorted(r.key() for r in records))


def cmd_add_record(ns: argparse.Namespace) -> int:
    creds = load_credentials(ns.env)
    sld, tld = ns.sld, ns.tld
    new = Record(
        name=ns.name, type=ns.type.upper(), address=ns.address, ttl=str(ns.ttl),
        mxpref=str(ns.mxpref) if ns.type.upper() == "MX" else None,
    )

    # 1. READ — capture live state, save it verbatim as the rollback source.
    before_xml = live_gethosts(creds, sld, tld)
    save_dir = Path(ns.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    before_path = save_dir / f"{sld}.{tld}.before.xml"
    before_path.write_text(before_xml)
    before = parse_hosts(before_xml)
    print(f"read {len(before)} live records → saved verbatim to {before_path}")

    # 2. GATE — never write if the mail records did not survive the read.
    assert_critical_present(before)
    print("critical mail records present: mail A, MX, SPF, DMARC, DKIM ✓")

    # 3. MODIFY — append exactly one, idempotently.
    after_intended, added = add_record(before, new)
    if not added:
        print(f"record already present, nothing to do: {new.key()}")
        return 0

    if ns.dry_run:
        params = build_sethosts_params(sld, tld, after_intended, creds)
        body = encode_body(params)
        assert_dkim_safe(after_intended, body)
        print(f"DRY RUN — would add exactly one record:\n  {new.key()}")
        print("encoding check passed (DKIM '+' → %2B). No write performed.")
        return 0

    # 4. WRITE the complete set + the one new record.
    live_sethosts(creds, sld, tld, after_intended)
    print("setHosts OK")

    # 5. VERIFY — re-read and prove exactly one addition, zero removals.
    after_xml = live_gethosts(creds, sld, tld)
    (save_dir / f"{sld}.{tld}.after.xml").write_text(after_xml)
    after = parse_hosts(after_xml)
    d = assert_clean_add(before, after, new)
    assert_critical_present(after)
    print(f"verified: +1 record, -0 records. Added: {d.added[0].key()}")
    return 0


def cmd_gethosts(ns: argparse.Namespace) -> int:
    creds = load_credentials(ns.env)
    xml = live_gethosts(creds, ns.sld, ns.tld)
    if ns.save:
        Path(ns.save).write_text(xml)
        print(f"saved {ns.save}")
    records = parse_hosts(xml)
    c = check_critical(records)
    print(f"{len(records)} records; critical present={c.all_present}")
    if not c.all_present:
        print(f"  MISSING: {', '.join(c.missing())}")
    print(_fmt(records))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env", default="/etc/namecheap-api.env",
                   help="path to the Namecheap API env file (auto-ash-1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gethosts", help="read-only: fetch and summarise the live set")
    g.add_argument("--sld", default="auto")
    g.add_argument("--tld", default="network")
    g.add_argument("--save", help="write the raw getHosts XML to this path")
    g.set_defaults(func=cmd_gethosts)

    a = sub.add_parser("add-record", help="append exactly one record, safely")
    a.add_argument("--sld", default="auto")
    a.add_argument("--tld", default="network")
    a.add_argument("--name", required=True, help="host, e.g. @ for the apex")
    a.add_argument("--type", required=True, help="A, TXT, MX, …")
    a.add_argument("--address", required=True)
    a.add_argument("--ttl", default=1800, type=int)
    a.add_argument("--mxpref", default=10, type=int)
    a.add_argument("--save-dir", default="/var/backups/namecheap",
                   help="where before/after record sets are saved (rollback source)")
    a.add_argument("--dry-run", action="store_true",
                   help="read + gate + encode-check only; never writes")
    a.set_defaults(func=cmd_add_record)

    ns = p.parse_args(argv)
    try:
        return ns.func(ns)
    except DnsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
