"""DNS-01 challenge broker for the delegated serve.auto.network zone.

The ONLY automated mutation path into the zone, and structurally unable
to write anything except TXT RRsets at
``_acme-challenge.<label>.serve.auto.network``: name grammar is validated
before any API call, rrtype is fixed, TTL is clamped, per-name values are
capped, and every present carries an expiry recorded in a local ledger a
systemd timer purges — a crashed ACME client can never strand a
challenge. Simultaneous apex+wildcard authorizations produce multiple
TXT values at one name; the broker always read-merges-writes the full
value set (design c880c5e6 §5.2).

Runs ONLY on the primary DNS box against the loopback PowerDNS API; the
api-key never leaves it. The argv/exit contract of ``main`` is the
frozen seam auto-bhs3c bridges registry control ops onto.

CLI:
    challenge_broker.py present  <name> <value> [--ttl N] [--expiry N]
    challenge_broker.py cleanup  <name> <value>
    challenge_broker.py purge-expired
Exit 0 on success, 2 on a refused request (bounds), 1 on API failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ZONE = "serve.auto.network"
CHALLENGE_PREFIX = "_acme-challenge."
TTL_FLOOR, TTL_CEILING = 60, 300
DEFAULT_TTL = 120
DEFAULT_EXPIRY = 900
MAX_VALUES_PER_NAME = 8
MAX_VALUE_LENGTH = 255

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class BrokerError(Exception):
    """A request outside the broker's bounds — refused before any API IO."""


def validate_challenge_name(name: str) -> str:
    """Return the serving label of a valid challenge name, else refuse."""
    if not isinstance(name, str) or len(name) > 253:
        raise BrokerError("challenge name is not a valid DNS name")
    suffix = "." + ZONE
    if not name.endswith(suffix):
        raise BrokerError(f"challenge name must end with {suffix}")
    head = name[: -len(suffix)]
    if not head.startswith(CHALLENGE_PREFIX):
        raise BrokerError(
            f"challenge name must start with {CHALLENGE_PREFIX}"
        )
    label = head[len(CHALLENGE_PREFIX):]
    if "." in label or _LABEL_RE.match(label) is None:
        raise BrokerError(
            "challenge name must be _acme-challenge.<label>." + ZONE
        )
    return label


def _quote(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_VALUE_LENGTH:
        raise BrokerError("challenge value must be 1..255 characters")
    if '"' in value or "\\" in value or "\n" in value:
        raise BrokerError("challenge value carries forbidden characters")
    return f'"{value}"'


class PdnsClient:
    """Thin loopback PowerDNS API client — full-RRset replacement only."""

    def __init__(self, api_url: str, api_key: str, server_id: str = "localhost"):
        self._base = (
            f"{api_url.rstrip('/')}/api/v1/servers/{server_id}/zones/{ZONE}."
        )
        self._key = api_key

    def _request(self, method: str, body: dict | None = None) -> dict | None:
        req = urllib.request.Request(
            self._base, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-API-Key": self._key,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def get_txt(self, name: str) -> list[str]:
        zone = self._request("GET")
        for rrset in zone.get("rrsets", []):
            if rrset["name"] == name + "." and rrset["type"] == "TXT":
                return [r["content"] for r in rrset["records"]]
        return []

    def replace_txt(self, name: str, values: list[str], ttl: int) -> None:
        rrset = {
            "name": name + ".",
            "type": "TXT",
            "ttl": ttl,
            "changetype": "REPLACE" if values else "DELETE",
        }
        if values:
            rrset["records"] = [
                {"content": v, "disabled": False} for v in values
            ]
        self._request("PATCH", {"rrsets": [rrset]})


class ChallengeBroker:
    def __init__(self, client, *, ledger_path, now_fn=time.time):
        self._client = client
        self._ledger_path = Path(ledger_path)
        self._now_fn = now_fn
        self.last_ttl: int | None = None

    # -- ledger ------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self._ledger_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, ledger: dict) -> None:
        # Drop empty name entries so the ledger mirrors live intent.
        ledger = {n: v for n, v in ledger.items() if v}
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True))
        tmp.replace(self._ledger_path)

    # -- operations ----------------------------------------------------------

    def present(self, name: str, value: str, *, ttl: int = DEFAULT_TTL,
                expiry: int = DEFAULT_EXPIRY) -> None:
        validate_challenge_name(name)
        quoted = _quote(value)
        ttl = max(TTL_FLOOR, min(TTL_CEILING, int(ttl)))
        self.last_ttl = ttl
        values = self._client.get_txt(name)
        if quoted not in values:
            if len(values) >= MAX_VALUES_PER_NAME:
                raise BrokerError(
                    f"refusing: {MAX_VALUES_PER_NAME} live values at {name}"
                )
            self._client.replace_txt(name, values + [quoted], ttl)
        ledger = self._load()
        ledger.setdefault(name, {})[value] = int(self._now_fn()) + int(expiry)
        self._save(ledger)

    def cleanup(self, name: str, value: str) -> None:
        validate_challenge_name(name)
        quoted = _quote(value)
        values = self._client.get_txt(name)
        if quoted in values:
            remaining = [v for v in values if v != quoted]
            self._client.replace_txt(name, remaining, DEFAULT_TTL)
        ledger = self._load()
        ledger.get(name, {}).pop(value, None)
        self._save(ledger)

    def purge_expired(self) -> int:
        """Remove every overdue value; returns the count removed from the
        ZONE (a ledger entry whose value is already gone reconciles
        silently). Idempotent."""
        now = int(self._now_fn())
        ledger = self._load()
        purged = 0
        for name in list(ledger):
            overdue = [
                value for value, expires_at in ledger[name].items()
                if expires_at < now
            ]
            if not overdue:
                continue
            values = self._client.get_txt(name)
            remaining = list(values)
            for value in overdue:
                quoted = f'"{value}"'
                if quoted in remaining:
                    remaining.remove(quoted)
                    purged += 1
                del ledger[name][value]
            if remaining != values:
                self._client.replace_txt(name, remaining, DEFAULT_TTL)
        self._save(ledger)
        return purged


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default="http://127.0.0.1:8081")
    parser.add_argument("--api-key-file",
                        default="/etc/autonomy-dns/api-key")
    parser.add_argument("--ledger",
                        default="/var/lib/autonomy-dns/challenges.json")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("present")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    p.add_argument("--expiry", type=int, default=DEFAULT_EXPIRY)
    c = sub.add_parser("cleanup")
    c.add_argument("name")
    c.add_argument("value")
    sub.add_parser("purge-expired")
    args = parser.parse_args(argv)

    api_key = Path(args.api_key_file).read_text().strip()
    broker = ChallengeBroker(
        PdnsClient(args.api_url, api_key), ledger_path=args.ledger
    )
    try:
        if args.op == "present":
            broker.present(args.name, args.value, ttl=args.ttl,
                           expiry=args.expiry)
        elif args.op == "cleanup":
            broker.cleanup(args.name, args.value)
        else:
            print(f"purged {broker.purge_expired()}")
    except BrokerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
