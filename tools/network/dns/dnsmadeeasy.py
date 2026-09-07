#!/usr/bin/env python3
"""Thin DNS Made Easy API client and CLI (v2.0, HMAC-signed).

Credentials come from the session's vault delivery, never argv:
  /run/secrets/dnsmadeeasy.api-key and /run/secrets/dnsmadeeasy.secret-key
  (``graph vault read dnsmadeeasy.api-key --org`` and ``…secret-key``), or the
  environment variables DNSMADEEASY_API_KEY / DNSMADEEASY_SECRET_KEY when a
  workspace row injects them (``credential:<org>:dnsmadeeasy.api-key``).

Usage:
  dnsmadeeasy.py domains                      # zones on the account (id, name, record count)
  dnsmadeeasy.py records <domain> [--type TXT] [--name sub]
  dnsmadeeasy.py add <domain> <name> <type> <value> [--ttl 60]
  dnsmadeeasy.py delete <domain> <record-id>
  dnsmadeeasy.py whoami                       # request signing check: lists nothing, proves auth
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("DNSMADEEASY_API_BASE", "https://api.dnsmadeeasy.com/V2.0")


def _read_secret(env: str, file_name: str) -> str:
    value = os.environ.get(env, "").strip()
    if value:
        return value
    path = Path("/run/secrets") / file_name
    if path.exists():
        return path.read_text().strip()
    sys.exit(
        f"dnsmadeeasy: no credential: set {env} or deliver it with "
        f"`graph vault read {file_name} --org`"
    )


def credentials() -> tuple[str, str]:
    return (
        _read_secret("DNSMADEEASY_API_KEY", "dnsmadeeasy.api-key"),
        _read_secret("DNSMADEEASY_SECRET_KEY", "dnsmadeeasy.secret-key"),
    )


def signed_headers(api_key: str, secret_key: str, now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(timezone.utc)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    digest = hmac.new(secret_key.encode(), stamp.encode(), hashlib.sha1).hexdigest()
    return {
        "x-dnsme-apiKey": api_key,
        "x-dnsme-requestDate": stamp,
        "x-dnsme-hmac": digest,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def request(method: str, path: str, body: dict | None = None, timeout: float = 30.0):
    api_key, secret_key = credentials()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=signed_headers(api_key, secret_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        sys.exit(f"dnsmadeeasy: HTTP {exc.code} on {method} {path}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"dnsmadeeasy: network error on {path}: {exc.reason}")


def domain_id(name: str) -> int:
    out = request("GET", "/dns/managed/name?domainname=" + urllib.parse.quote(name))
    return int(out["id"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dnsmadeeasy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    sub.add_parser("domains")
    r = sub.add_parser("records"); r.add_argument("domain"); r.add_argument("--type"); r.add_argument("--name")
    a = sub.add_parser("add"); a.add_argument("domain"); a.add_argument("name"); a.add_argument("type"); a.add_argument("value"); a.add_argument("--ttl", type=int, default=60)
    d = sub.add_parser("delete"); d.add_argument("domain"); d.add_argument("record_id", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "whoami":
        out = request("GET", "/dns/managed/?rows=1")
        print(f"dnsmadeeasy: auth ok · {out.get('totalRecords', '?')} zones on the account")
        return 0
    if args.cmd == "domains":
        out = request("GET", "/dns/managed/")
        for z in sorted(out.get("data", []), key=lambda z: z.get("name", "")):
            print(f"{z.get('id'):>10}  {z.get('name'):40s}  records={z.get('recordCount', '?')}  gtd={z.get('gtdEnabled')}")
        print(f"{out.get('totalRecords', len(out.get('data', [])))} zones")
        return 0
    if args.cmd == "records":
        did = domain_id(args.domain)
        q = []
        if args.type: q.append("type=" + urllib.parse.quote(args.type))
        if args.name: q.append("recordName=" + urllib.parse.quote(args.name))
        out = request("GET", f"/dns/managed/{did}/records" + ("?" + "&".join(q) if q else ""))
        for rec in out.get("data", []):
            print(f"{rec.get('id'):>10}  {rec.get('type'):6s} {rec.get('name') or '@':30s} ttl={rec.get('ttl'):<6} {rec.get('value')}")
        print(f"{out.get('totalRecords', len(out.get('data', [])))} records")
        return 0
    if args.cmd == "add":
        did = domain_id(args.domain)
        out = request("POST", f"/dns/managed/{did}/records/", {
            "name": args.name, "type": args.type.upper(), "value": args.value, "ttl": args.ttl, "gtdLocation": "DEFAULT",
        })
        print(f"added record {out.get('id')}: {out.get('type')} {out.get('name') or '@'} → {out.get('value')} (ttl {out.get('ttl')})")
        return 0
    if args.cmd == "delete":
        did = domain_id(args.domain)
        request("DELETE", f"/dns/managed/{did}/records/{args.record_id}")
        print(f"deleted record {args.record_id} from {args.domain}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
