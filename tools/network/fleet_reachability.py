"""Fleet peer reachability over the auto.network registry.

A fleet machine advertises where roster peers can reach its sync listener
(``node:announce``) and discovers where to reach other roster machines
(``node:lookup``), authorized by its reachability delegation cert
(root -> machine_key, scope ``node:announce``/``node:lookup``, org = the
personal org's registered ``org_uuid``).

These are discovery hints, never authority: the roster authorizes membership;
a hint only says where a roster machine currently is, keyed by
``(org_uuid, machine_pub)`` and expiring by TTL. The machine identity IS the
envelope signer, so a machine can only ever announce itself.
"""

from __future__ import annotations

import time as _time
from typing import Iterable, Mapping, Optional, Sequence

from tools.network.idkit import KeyPair
from tools.network.idkit.certs import DelegationCert
from tools.network.registry.signing import sign_request


def _send(client, registry_url, method, path, key, payload, cert, ts, timeout):
    envelope = sign_request(
        key, method, path, payload,
        ts=int(_time.time()) if ts is None else ts, cert=cert,
    )
    url = f"{registry_url.rstrip('/')}{path}"
    if client is not None:  # injected (tests): an httpx.Client-like .request
        return client.request(method, url, json=envelope)
    import httpx

    with httpx.Client(timeout=timeout) as c:
        return c.request(method, url, json=envelope)


def announce(
    registry_url: str,
    org_uuid: str,
    machine_key: KeyPair,
    cert: DelegationCert,
    addrs: Sequence[str],
    *,
    ttl: Optional[int] = None,
    relay_url: Optional[str] = None,
    ts: Optional[int] = None,
    timeout: float = 10.0,
    client=None,
) -> dict:
    """Publish this machine's reachability hint (node:announce). Returns JSON."""
    path = f"/v1/orgs/{org_uuid}/reachability"
    payload: dict = {"addrs": list(addrs)}
    if ttl is not None:
        payload["ttl"] = ttl
    if relay_url is not None:
        payload["relay_url"] = relay_url
    resp = _send(client, registry_url, "POST", path, machine_key, payload, cert, ts, timeout)
    resp.raise_for_status()
    return resp.json()


def lookup_hints(
    registry_url: str,
    org_uuid: str,
    machine_key: KeyPair,
    cert: DelegationCert,
    node_pubs: Iterable[str],
    *,
    ts: Optional[int] = None,
    timeout: float = 10.0,
    client=None,
) -> dict:
    """node:lookup each pub -> ``{machine_pub: {"addrs": [...], "relay_url": ...}}``.

    ``addrs`` are direct-dial candidates; ``relay_url`` is the peer's own
    standing relay route (None when it announced none). Best-effort per
    peer: a peer that is unreachable, unannounced, or errors is simply
    omitted, so one bad peer never blocks discovery of the others.
    """
    out: dict = {}
    path = f"/v1/orgs/{org_uuid}/reachability/query"
    for pub in node_pubs:
        try:
            resp = _send(client, registry_url, "POST", path, machine_key,
                         {"node": pub}, cert, ts, timeout)
            resp.raise_for_status()
            hints = resp.json().get("hints", [])
        except Exception:
            continue
        cands = []
        relay_url = None
        for hint in hints:
            for addr in (hint.get("addrs") or []):
                if isinstance(addr, str) and addr:
                    cands.append(addr)
            candidate = hint.get("relay_url")
            if isinstance(candidate, str) and candidate and relay_url is None:
                relay_url = candidate
        if cands or relay_url:
            out[pub] = {"addrs": cands, "relay_url": relay_url}
    return out


def lookup(
    registry_url: str,
    org_uuid: str,
    machine_key: KeyPair,
    cert: DelegationCert,
    node_pubs: Iterable[str],
    *,
    ts: Optional[int] = None,
    timeout: float = 10.0,
    client=None,
) -> dict:
    """node:lookup each pub -> ``{machine_pub: [ws candidate urls]}`` (direct only)."""
    hints = lookup_hints(registry_url, org_uuid, machine_key, cert, node_pubs,
                         ts=ts, timeout=timeout, client=client)
    return {pub: h["addrs"] for pub, h in hints.items() if h["addrs"]}


class ReachabilityCache:
    """Throttled ``node:announce`` + ``node:lookup`` feeding ``peer_addresses``.

    The sync scheduler calls :meth:`peers` every poll, so it must not block on
    the registry each tick. This refreshes at most once per ``interval`` seconds
    — announcing this machine's own address, then resolving the roster — and
    returns the last resolved map in between. Every registry call is best-effort:
    a failure keeps the previous map rather than emptying it.

    All discovery inputs are read through getters at refresh time, so a roster
    change, a late org registration, or a re-unlock that supplies the cert are
    all picked up on the next refresh without rebuilding the cache.
    """

    def __init__(
        self,
        *,
        binding_getter,
        machine_key_getter,
        cert_getter,
        roster_getter,
        advertise_addrs,
        relay_url=None,
        ttl: int = 300,
        interval: float = 45.0,
        timeout: float = 3.0,
        ts=None,
        clock=None,
        client=None,
    ):
        self._binding_getter = binding_getter
        self._machine_key_getter = machine_key_getter
        self._cert_getter = cert_getter
        self._roster_getter = roster_getter
        # A list is frozen at construction; a callable is re-read at every
        # refresh, so an operator who sets the advertised URLs after unlock
        # is announced on the next interval without re-arming the runtime.
        self._advertise_addrs = (
            advertise_addrs if callable(advertise_addrs)
            else list(advertise_addrs or [])
        )
        #: This machine's standing relay route to announce beside its direct
        #: addresses: a string, a callable re-read each refresh, or None.
        self._relay_url = relay_url
        self._ttl = ttl
        self._interval = interval
        self._timeout = timeout
        self._ts = ts
        self._clock = clock or _time.monotonic
        self._client = client
        self._peers: dict = {}
        self._hints: dict = {}
        self._last: Optional[float] = None
        #: (monotonic, addrs) of the last successful announce, for status.
        self.last_announce: Optional[tuple[float, list]] = None

    def advertised_addrs(self) -> list:
        """The URLs this machine currently advertises (fresh if a getter)."""
        try:
            value = (
                self._advertise_addrs() if callable(self._advertise_addrs)
                else self._advertise_addrs
            )
        except Exception:
            return []
        return [a for a in (value or []) if isinstance(a, str) and a]

    def peers(self) -> dict:
        self._maybe_refresh()
        return dict(self._peers)

    def snapshot(self) -> dict:
        """The last resolved peer map WITHOUT triggering a refresh (status)."""
        return dict(self._peers)

    def relay_routes(self) -> dict:
        """``{machine_pub: relay_url}`` for peers that announced a standing
        relay route (refreshing on the same throttle as :meth:`peers`)."""
        self._maybe_refresh()
        return {
            pub: h["relay_url"] for pub, h in self._hints.items()
            if h.get("relay_url")
        }

    def announced_relay_url(self) -> Optional[str]:
        try:
            value = self._relay_url() if callable(self._relay_url) else self._relay_url
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    def _maybe_refresh(self) -> None:
        now = self._clock()
        if self._last is not None and (now - self._last) < self._interval:
            return
        self._last = now

        binding = self._binding_getter()
        key = self._machine_key_getter()
        cert = self._cert_getter()
        if not binding or key is None or cert is None:
            return
        registry_url = binding.get("registry_url")
        org_uuid = binding.get("org_uuid")
        if not registry_url or not org_uuid:
            return

        advertise = self.advertised_addrs()
        relay_url = self.announced_relay_url()
        if advertise or relay_url:
            try:
                announce(registry_url, org_uuid, key, cert, advertise,
                         ttl=self._ttl, relay_url=relay_url, ts=self._ts,
                         timeout=self._timeout, client=self._client)
                self.last_announce = (now, list(advertise))
            except Exception:
                pass  # keep serving the last map; retry next interval

        try:
            own = key.public_hex
            pubs = [p for p in self._roster_getter() if p != own]
        except Exception:
            return
        try:
            hints = lookup_hints(registry_url, org_uuid, key, cert, pubs,
                                 ts=self._ts, timeout=self._timeout,
                                 client=self._client)
        except Exception:
            return
        self._hints = hints
        self._peers = {pub: h["addrs"] for pub, h in hints.items() if h["addrs"]}
