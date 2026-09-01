"""Dashboard-side client for the frozen RelayKit DNS-01 control contract.

This module owns no DNS credentials and no record names.  It signs one
persona-scoped operation, sends it through the already-authenticated serving
connector, and accepts only the registry-derived challenge name.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from tools.network.idkit import (
    DelegationCert,
    KeyPair,
    canonical_json,
    verify_signature,
)


CAPABILITY = "dns-01/1"
SIGNING_DOMAIN = b"autonomy.network.serve.dns01.v1\n"


class Dns01Error(RuntimeError):
    pass


class Dns01Unavailable(Dns01Error):
    pass


class Dns01Refused(Dns01Error):
    pass


@dataclass(frozen=True)
class Dns01Authority:
    key: KeyPair
    cert: DelegationCert

    def __post_init__(self) -> None:
        if self.key.public_hex != self.cert.child_pub:
            raise ValueError("DNS-01 key does not match delegated certificate")
        if tuple(self.cert.scope) != ("serve:dns-01",):
            raise ValueError("DNS-01 certificate scope must be exactly serve:dns-01")
        if self.cert.parent_cert is not None:
            raise ValueError("DNS-01 certificate must be issued directly by the org root")
        if self.cert.subject.kind != "persona":
            raise ValueError("DNS-01 certificate subject must be a persona")


def _signed_args(op: str, fields: dict, authority: Dns01Authority) -> dict:
    core = {"op": op, **fields}
    return {
        **fields,
        "cert": authority.cert.to_json().decode("ascii"),
        "sig": authority.key.sign_hex(SIGNING_DOMAIN + canonical_json(core)),
    }


def verify_request_signature(op: str, args: dict, public_hex: str) -> bool:
    """Test/evidence helper: verify exactly the signed fields for one op."""
    excluded = {"cert", "sig"}
    core = {"op": op, **{k: v for k, v in args.items() if k not in excluded}}
    try:
        verify_signature(
            public_hex,
            args["sig"],
            SIGNING_DOMAIN + canonical_json(core),
        )
        return True
    except Exception:
        return False


class Dns01Client:
    def __init__(
        self,
        org: str,
        authority: Dns01Authority,
        *,
        control: Callable[[str, str, dict], dict] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        if control is None:
            from tools.dashboard.link_serving_supervisor import control
        self._org = org
        self._authority = authority
        self._control = control
        self._now = now

    def _require_capability(self) -> None:
        try:
            status = self._control(self._org, "connector-status", {})
        except Exception as exc:
            raise Dns01Unavailable("serving connector is unavailable") from exc
        if status.get("ok") is not True or status.get("serving") is not True:
            raise Dns01Unavailable("serving connector is unavailable")
        if CAPABILITY not in status.get("accepted_caps", ()):
            raise Dns01Unavailable("relay did not negotiate dns-01/1")

    def _send(self, op: str, fields: dict) -> dict:
        self._require_capability()
        reply = self._control(
            self._org, op, _signed_args(op, fields, self._authority),
        )
        if reply.get("ok") is not True:
            raise Dns01Refused("DNS-01 operation refused")
        return reply

    def present(
        self,
        order: str,
        value: str,
        *,
        ttl: int = 60,
        lifetime: int = 600,
    ) -> dict:
        ts = int(self._now())
        reply = self._send("serve.dns01.present", {
            "order": order,
            "value": value,
            "ttl": ttl,
            "expiry": ts + lifetime,
            "ts": ts,
        })
        return {"name": reply["name"], "expires_at": reply["expires_at"]}

    def cleanup(self, order: str, value: str) -> None:
        self._send("serve.dns01.cleanup", {
            "order": order,
            "value": value,
            "ts": int(self._now()),
        })
