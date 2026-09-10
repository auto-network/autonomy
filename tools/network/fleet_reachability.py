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

import logging
import threading
import time as _time
from urllib.parse import urlsplit
from typing import Iterable, Mapping, Optional, Sequence

from tools.network.idkit import KeyPair
from tools.network.idkit.certs import DelegationCert
from tools.network.registry.signing import sign_request

logger = logging.getLogger(__name__)


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


def drop_self_reachable(candidates, own_addrs) -> list:
    """Remove peer candidates that would reach THIS machine.

    A hint says where a PEER is. An address that resolves to this machine is
    not that, whatever the peer believes, and dialing it is guaranteed to
    reach the wrong host. This is the one exclusion reachability may make,
    because it is a statement about ourselves rather than a judgement about
    the peer: §1 keeps reachability out of authority, and refusing to dial
    ourselves decides nothing about whether the peer is a member or is up.

    Observed 2026-09-09: sjc-2 dialled toward home, reached itself, and the
    fleet handshake refused it 24 times between 19:35:47Z and 20:00:21Z --
    "expected 3996513b6b233afd, got 571d62ab69c59f83", the second being
    sjc-2's own key. The refusal is correct and it is not free: a connect and
    a full handshake round trip are spent per attempt, and the log reads as a
    peer-identity problem when the cause is an address meaning different
    things on two machines.

    HOST, not host:port. An address on this machine's own host reaches this
    machine whatever port it names, so a peer hint pointing at our host on a
    different port is equally wrong.

    Assumes NOTHING about subnets, which is what makes it safe. The compose
    subnet is pinned per host by a deterministic preflight, so a collision
    between two nodes is the expected case and not an invariant --
    "a design that ASSUMES collision is wrong; a design that assumes
    NON-collision is wrong more often". Comparing against our own addresses
    is exact: it is true precisely when it is true.

    An empty ``own_addrs`` filters nothing. A machine that does not know its
    own addresses must not start discarding a peer's.
    """
    mine = set()
    for addr in own_addrs or ():
        if not isinstance(addr, str) or not addr:
            continue
        try:
            host = urlsplit(addr).hostname
        except ValueError:
            continue
        if host:
            mine.add(host)
    if not mine:
        return list(candidates or ())
    kept = []
    for addr in candidates or ():
        if not isinstance(addr, str) or not addr:
            continue
        try:
            host = urlsplit(addr).hostname
        except ValueError:
            continue
        if host and host in mine:
            logger.info(
                "fleet reachability: not dialing %s for a peer -- that address "
                "reaches this machine", addr,
            )
            continue
        kept.append(addr)
    return kept


def announce(
    registry_url: str,
    org_uuid: str,
    machine_key: KeyPair,
    cert: DelegationCert,
    addrs: Sequence[str],
    *,
    ttl: Optional[int] = None,
    relay_url: Optional[str] = None,
    descriptor: Optional[Mapping] = None,
    ts: Optional[int] = None,
    timeout: float = 10.0,
    client=None,
) -> dict:
    """Publish this machine's reachability hint (node:announce). Returns JSON.

    ``descriptor`` carries the machine-signed reachability descriptor
    (``fleet_descriptor``). The registry stores and returns it verbatim and
    **does not re-sign it** (contract §3), so the signature that arrives at a
    peer is the one this machine made, and the registry is a cache rather than
    an authority. Omitted, this is the pre-descriptor announce unchanged.
    """
    path = f"/v1/orgs/{org_uuid}/reachability"
    payload: dict = {"addrs": list(addrs)}
    if ttl is not None:
        payload["ttl"] = ttl
    if relay_url is not None:
        payload["relay_url"] = relay_url
    if descriptor is not None:
        payload["descriptor"] = dict(descriptor)
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
        descriptor = None
        for hint in hints:
            for addr in (hint.get("addrs") or []):
                if isinstance(addr, str) and addr:
                    cands.append(addr)
            candidate = hint.get("relay_url")
            if isinstance(candidate, str) and candidate and relay_url is None:
                relay_url = candidate
            if descriptor is None:
                descriptor = _verified_descriptor(hint.get("descriptor"), pub)
        if cands or relay_url or descriptor:
            out[pub] = {
                "addrs": cands, "relay_url": relay_url,
                "descriptor": descriptor,
            }
    return out


def _verified_descriptor(raw: object, expected_machine_pub: str):
    """Verify a cached descriptor, or return None.

    The registry does not re-sign (contract §3), so the caller is the only
    thing standing between a cache and a forged hint. Two checks, and neither
    is authority: the signature must be the one the NAMED machine made, and
    the name must be the peer we asked about -- otherwise a registry could
    answer a lookup for A with a validly-signed descriptor for B.

    Whether that machine is a fleet member is the ROSTER's answer and is
    checked elsewhere. §1: reachability is never authority.

    A descriptor that fails either check is dropped rather than raised on: one
    bad cached entry must not deny discovery of a peer whose direct addresses
    are fine, which is the same best-effort rule the rest of this function
    already follows.
    """
    if raw is None:
        return None
    from tools.network import fleet_descriptor

    try:
        verified = fleet_descriptor.verify(raw)
    except Exception:
        logger.warning(
            "fleet reachability: discarding an unverifiable descriptor "
            "cached for %s", expected_machine_pub[:12],
        )
        return None
    if verified["machine_pub"] != expected_machine_pub:
        logger.warning(
            "fleet reachability: descriptor cached for %s is signed by %s; "
            "discarding", expected_machine_pub[:12],
            verified["machine_pub"][:12],
        )
        return None
    return verified


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
        background: bool = False,
        min_lookup_spacing: float = 15.0,
    ):
        """``background=True`` (the dashboard): a due refresh runs on its own
        daemon thread and the caller gets the last map at once, so registry
        latency never touches the event loop (three 16 s dashboard stalls,
        2026-09-07, auto-8dw0w). ``False`` (tests, CLIs) refreshes inline.

        Cadence (auto-8dw0w): announce on boot, when the advertised set or
        relay route changes, and as a keepalive every ttl/2 (hints expire at
        ``ttl``); look up when a roster peer has no address, when the
        scheduler reported a failed pull for one (:meth:`note_failed`), or
        every ``interval`` -- never more often than ``min_lookup_spacing``.
        """
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
        # Log on CHANGE only: the cache refreshes every 45s for the life of
        # the process, and a silent failure here left two machines with
        # open ports and no idea why neither ever dialed the other
        # (2026-09-06). One line per state transition, not per tick.
        self._logged: dict = {}      # slot -> last logged state
        self._logged_error: Optional[str] = None
        self._logged_peers: Optional[str] = None
        #: (monotonic, addrs) of the last successful announce, for status.
        self.last_announce: Optional[tuple[float, list]] = None
        self._background = background
        self._min_lookup_spacing = min_lookup_spacing
        #: What was last announced successfully: (addrs, relay_url).
        self._announced: Optional[tuple[tuple, Optional[str]]] = None
        #: The last descriptor published and the content it described, so a
        #: keepalive re-announces it instead of minting a new generation for
        #: reachability that has not moved.
        self._descriptor: Optional[dict] = None
        self._descriptor_for_content = None
        #: Highest generation seen in any projection of THIS machine's own
        #: descriptor; repairs a counter rebuilt behind a surviving identity.
        self._observed_own_generation = 0
        self._last_lookup: Optional[float] = None
        #: Roster peers the scheduler reported a failed pull for since the
        #: last lookup: they are looked up again at the next opportunity.
        self._stale: set = set()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None


    def _descriptor_for(self, key, advertise, wanted):
        """This machine's signed descriptor, minting a generation only when the
        CONTENT changes.

        A keepalive re-announces the same descriptor rather than a new one. The
        announce cadence is TTL/2, so minting per announce would burn a
        generation every couple of minutes forever and make ordering churn on a
        machine whose reachability had not moved at all -- readers would see a
        stream of "newer" descriptors carrying identical addresses.

        A build failure returns None and the announce proceeds without a
        descriptor: the pre-descriptor announce still works, and a peer that
        cannot publish one must not thereby become unannounced.
        """
        from tools.network import fleet_descriptor

        if self._descriptor is not None and self._descriptor_for_content == wanted:
            return self._descriptor
        try:
            generation = fleet_descriptor.next_generation(
                observed=self._observed_own_generation,
            )
            built = fleet_descriptor.build(
                key,
                # The reachability cert binds this key to the roster machine
                # identity (fleet_runtime.py:66-67), so the signer and the row
                # key are the same durable identity by construction.
                machine_pub=key.public_hex,
                addresses=advertise,
                generation=generation,
            )
        except Exception as exc:
            self._error(f"descriptor build failed: {exc!r}")
            return None
        self._descriptor = built
        self._descriptor_for_content = wanted
        return built

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

    def note_failed(self, machine_pub: str) -> None:
        """The scheduler could not pull *machine_pub* at its known address:
        look that peer up again at the next opportunity."""
        with self._lock:
            self._stale.add(machine_pub)

    def wait_idle(self, timeout: float = 10.0) -> None:
        """Block until a background refresh in flight has finished (tests)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

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

    def _due(self, now: float) -> bool:
        """Whether anything needs the registry now (see the constructor)."""
        if self._last is None:
            return True
        if (now - self._last) >= self._interval:
            return True
        announced = self._announced
        if self.last_announce is not None and (now - self.last_announce[0]) >= self._ttl / 2:
            return True
        if announced is not None and announced != (
            tuple(self.advertised_addrs()), self.announced_relay_url()
        ):
            return True
        last_lookup = self._last_lookup
        spaced = last_lookup is None or (now - last_lookup) >= self._min_lookup_spacing
        if not spaced:
            return False
        if self._stale:
            return True
        try:
            own = self._machine_key_getter()
            own_pub = own.public_hex if own is not None else None
            missing = [p for p in self._roster_getter() if p != own_pub and p not in self._peers]
        except Exception:
            missing = []
        return bool(missing)

    def _maybe_refresh(self) -> None:
        now = self._clock()
        if not self._due(now):
            return
        if not self._background:
            self._last = now
            self._refresh(now)
            return
        with self._lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return
            self._last = now
            worker = threading.Thread(
                target=self._refresh, args=(now,),
                name="fleet-reachability-refresh", daemon=True,
            )
            self._worker = worker
        worker.start()

    def _refresh(self, now: float) -> None:
        try:
            self._refresh_inner(now)
        except Exception as exc:  # never let a refresh thread die loudly
            self._error(f"refresh failed: {exc!r}")

    def _refresh_inner(self, now: float) -> None:
        binding = self._binding_getter()
        key = self._machine_key_getter()
        cert = self._cert_getter()
        if not binding or key is None or cert is None:
            self._state(
                "cred", "inactive:no-credential",
                "fleet reachability inactive: %s -- direct-tier discovery "
                "cannot announce or look up peers until the personal org is "
                "registered and an unlock delivers the reachability cert",
                "no registry binding" if not binding
                else "credential carries no reachability key/cert",
            )
            return
        registry_url = binding.get("registry_url")
        org_uuid = binding.get("org_uuid")
        if not registry_url or not org_uuid:
            self._state(
                "cred", "inactive:no-org",
                "fleet reachability inactive: binding has no registry_url/org_uuid",
            )
            return
        self._state("cred", "active", "fleet reachability active: registry=%s org=%s",
                    registry_url, org_uuid[:8])

        advertise = self.advertised_addrs()
        relay_url = self.announced_relay_url()
        wanted = (tuple(advertise), relay_url)
        descriptor = self._descriptor_for(key, advertise, wanted)
        keepalive_due = (
            self.last_announce is None
            or (now - self.last_announce[0]) >= self._ttl / 2
        )
        if (advertise or relay_url) and (self._announced != wanted or keepalive_due):
            try:
                announce(registry_url, org_uuid, key, cert, advertise,
                         ttl=self._ttl, relay_url=relay_url,
                         descriptor=descriptor, ts=self._ts,
                         timeout=self._timeout, client=self._client)
                self.last_announce = (now, list(advertise))
                self._announced = wanted
                self._error(None)
                self._state(
                    "announce",
                    "announced:" + ",".join(advertise) + "|" + (relay_url or ""),
                    "fleet reachability announced addrs=%s relay_route=%s",
                    advertise, bool(relay_url),
                )
            except Exception as exc:
                # keep serving the last map; retry next interval
                self._error(f"announce failed: {exc!r}")
        elif not (advertise or relay_url):
            self._state("announce", "nothing",
                        "fleet reachability: nothing to announce (no advertised "
                        "addresses, no standing route) -- peers cannot dial this "
                        "machine directly")

        try:
            own = key.public_hex
            pubs = [p for p in self._roster_getter() if p != own]
        except Exception as exc:
            self._error(f"roster read failed: {exc!r}")
            return
        # Which peers to look up: all of them on a full interval, otherwise
        # only those with no address or a reported failed pull.
        full = self._last_lookup is None or (now - self._last_lookup) >= self._interval
        with self._lock:
            stale, self._stale = self._stale, set()
        targets = pubs if full else [
            p for p in pubs if p not in self._peers or p in stale
        ]
        if not targets:
            return
        try:
            hints = lookup_hints(registry_url, org_uuid, key, cert, targets,
                                 ts=self._ts, timeout=self._timeout,
                                 client=self._client)
        except Exception as exc:
            with self._lock:
                self._stale |= stale
            self._error(f"lookup failed: {exc!r}")
            return
        self._last_lookup = now
        if full:
            merged_hints = dict(hints)
        else:
            merged_hints = dict(self._hints)
            for pub in targets:
                if pub in hints:
                    merged_hints[pub] = hints[pub]
                else:
                    merged_hints.pop(pub, None)
        hints = merged_hints
        self._hints = hints
        # A hint that points at THIS machine is not where the peer is. Dropped
        # here, once, so every consumer of peers() is spared the wasted connect
        # and handshake rather than each having to know.
        own = self.advertised_addrs()
        self._peers = {
            pub: kept
            for pub, h in hints.items()
            for kept in (drop_self_reachable(h["addrs"], own),)
            if kept
        }
        summary = "; ".join(
            f"{pub[:12]} addrs={h['addrs']} relay_route={bool(h.get('relay_url'))}"
            for pub, h in sorted(hints.items())
        ) or "none"
        if summary != self._logged_peers:
            self._logged_peers = summary
            logger.info(
                "fleet reachability resolved %d of %d roster peer(s): %s",
                len(hints), len(pubs), summary,
            )

    def _state(self, slot: str, state: str, message: str, *args) -> None:
        if self._logged.get(slot) != state:
            self._logged[slot] = state
            logger.info(message, *args)

    def _error(self, error: Optional[str]) -> None:
        if error != self._logged_error:
            self._logged_error = error
            if error is not None:
                logger.warning("fleet reachability %s", error)
