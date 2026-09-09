"""A member node's network-fabric runtime (G1).

Composes the pieces a node needs to be reachable at every rung of the
fallback chain, behind ONE application handler:

- a :class:`~.direct.DirectChannelServer` when the node can accept
  inbound connections (the direct rung);
- a B2 :class:`~.connector.TunnelConnector` parked at the auto.network
  floor (always — the floor is what makes connectivity guaranteed);
- a :class:`~.peer.PeerParkConnector` per org peer relay, each verified
  to hold ``relay:serve`` before the node parks there;
- a reachability announcer that registers the node's hints with the
  registry and refreshes them on a heartbeat (stale hints must die, so
  liveness is a *lease*, not a record).

Runnable directly (this is what the three-process acceptance test
drives)::

    python -m tools.network.relaykit.node \
        --org <uuid> --root-pub <64 hex> \
        --key-file node.hex --cert-file node.cert \
        --listen-port 9410 \
        --floor ws://127.0.0.1:8477 \
        --peer-relay ws://127.0.0.1:9420 \
        --registry http://127.0.0.1:8477 \
        --announce-addr ws://127.0.0.1:9410 --announce-ttl 600
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from typing import Optional

from tools.network.idkit import DelegationCert, KeyPair

from .connector import TunnelConnector, echo_handler, file_handler
from .direct import DirectChannelServer
from .peer import PeerParkConnector


class NodeServer:
    def __init__(
        self,
        org: str,
        root_pub: str,
        key: KeyPair,
        cert: DelegationCert,
        handler=echo_handler,
        *,
        floor_key: KeyPair | None = None,
        floor_cert: DelegationCert | None = None,
        floor_channel_cert: DelegationCert | None = None,
        listen_host: str = "127.0.0.1",
        listen_port: Optional[int] = None,
        floor_url: Optional[str] = None,
        peer_relay_urls: tuple = (),
        registry_url: Optional[str] = None,
        announce_addrs: tuple = (),
        announce_relay_url: Optional[str] = None,
        announce_ttl: int = 3600,
        min_backoff: float = 0.2,
        max_backoff: float = 5.0,
        machine_key: KeyPair | None = None,
    ):
        # The base node certificate is emitted to direct/peer viewers. A
        # persona-bearing certificate belongs only in registry admission and
        # must never enter any viewer SERVER_HELLO, regardless of whether
        # reachability announcement happens to be enabled for this instance.
        if cert.subject.kind == "persona":
            raise ValueError(
                "persona-bearing registry credentials cannot be used as a "
                "NodeServer viewer credential; use an identity-neutral node "
                "certificate and a separate floor registry certificate"
            )
        self._org = org
        self._root_pub = root_pub
        self._key = key
        self._cert = cert
        self._floor_key = floor_key or key
        self._floor_cert = floor_cert or cert
        self._floor_channel_cert = floor_channel_cert or cert
        if self._floor_cert.child_pub != self._floor_key.public_hex:
            raise ValueError("floor cert does not match the floor serving key")
        if self._floor_channel_cert.child_pub != self._floor_key.public_hex:
            raise ValueError("floor channel cert does not match the floor serving key")
        if (
            registry_url is not None
            and floor_url is not None
            and self._floor_key.public_hex == key.public_hex
        ):
            raise ValueError(
                "a reachability-announcing node must use a distinct floor "
                "serving key so the registry cannot join persona to addresses"
            )
        self._handler = handler
        self._direct = (
            DirectChannelServer(org, key, cert, handler,
                                host=listen_host, port=listen_port)
            if listen_port is not None else None
        )
        #: This node's machine identity for its outbound tunnels. Ephemeral
        #: unless the caller supplies one — the node is a peer relay, not an
        #: enrolled fleet machine.
        self._machine_key = machine_key or KeyPair.generate()
        self._connectors = []
        if floor_url:
            # Every tunnel names a machine. The floor tunnel is this node's
            # own serving tunnel, so it carries a machine identity like any
            # other; an unnamed one would share a single relay slot with every
            # other unnamed tunnel of the org and they would replace each
            # other.
            self._connectors.append(TunnelConnector(
                floor_url, org, self._floor_key, self._floor_cert, handler,
                channel_cert=self._floor_channel_cert,
                machine_key=self._machine_key,
                min_backoff=min_backoff, max_backoff=max_backoff,
            ))
        for relay_url in peer_relay_urls:
            self._connectors.append(PeerParkConnector(
                relay_url, org, key, cert, handler, root_pub=root_pub,
                min_backoff=min_backoff, max_backoff=max_backoff,
            ))
        self._registry_url = registry_url
        self._announce_addrs = list(announce_addrs)
        self._announce_relay_url = announce_relay_url
        self._announce_ttl = announce_ttl
        self._tasks: list = []

    def _announce_once(self) -> None:
        """Register/refresh this node's hints (lazy imports: registry
        client bits are only needed when discovery goes through it)."""
        import time as _time

        import httpx

        from tools.network.registry.signing import sign_request

        path = f"/v1/orgs/{self._org}/reachability"
        payload = {"addrs": self._announce_addrs, "ttl": self._announce_ttl}
        if self._announce_relay_url:
            payload["relay_url"] = self._announce_relay_url
        envelope = sign_request(self._key, "POST", path, payload,
                                ts=int(_time.time()), cert=self._cert)
        httpx.post(f"{self._registry_url.rstrip('/')}{path}", json=envelope,
                   timeout=10.0).raise_for_status()

    async def _announce_loop(self) -> None:
        interval = max(30.0, self._announce_ttl / 2)
        while True:
            try:
                await asyncio.to_thread(self._announce_once)
            except Exception:
                pass  # registry unreachable: hints just go stale, retry
            await asyncio.sleep(interval)

    async def start(self) -> None:
        if self._direct is not None:
            await self._direct.start()
        for connector in self._connectors:
            self._tasks.append(asyncio.create_task(connector.run()))
        if self._registry_url is not None:
            self._tasks.append(asyncio.create_task(self._announce_loop()))

    async def stop(self) -> None:
        for connector in self._connectors:
            connector.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        if self._direct is not None:
            await self._direct.stop()

    async def run_forever(self) -> None:
        await self.start()
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="auto.network fabric node")
    parser.add_argument("--org", required=True)
    parser.add_argument("--root-pub", required=True, help="org root public key, 64 hex")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--cert-file", required=True)
    parser.add_argument(
        "--floor-cert-file",
        help="persona-bearing registry-admission cert for the floor tunnel",
    )
    parser.add_argument(
        "--floor-key-file",
        help="distinct serving key for the floor tunnel",
    )
    parser.add_argument(
        "--floor-channel-cert-file",
        help="identity-neutral viewer cert over the floor serving key",
    )
    parser.add_argument("--mode", choices=["echo", "serve-file"], default="echo")
    parser.add_argument("--file", help="file to serve (serve-file mode)")
    parser.add_argument("--content-type", default="text/html")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int,
                        help="direct-dial listener port (omit to not listen)")
    parser.add_argument("--floor", help="central relay base URL (ws://...)")
    parser.add_argument("--peer-relay", action="append", default=[],
                        help="org peer relay base URL (repeatable)")
    parser.add_argument("--registry", help="registry base URL for hint announcements")
    parser.add_argument("--announce-addr", action="append", default=[],
                        help="direct-dial candidate to announce (repeatable)")
    parser.add_argument("--announce-relay-url",
                        help="peer-relay dial URL to announce for this node")
    parser.add_argument("--announce-ttl", type=int, default=3600)
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())
    floor_key = key
    floor_cert = cert
    floor_channel_cert = cert
    floor_args = (
        args.floor_key_file,
        args.floor_cert_file,
        args.floor_channel_cert_file,
    )
    if any(floor_args) and not all(floor_args):
        parser.error(
            "--floor-key-file, --floor-cert-file, and "
            "--floor-channel-cert-file must be supplied together"
        )
    if args.floor_key_file:
        with open(args.floor_key_file) as fh:
            floor_key = KeyPair.from_private_hex(fh.read().strip())
    if args.floor_cert_file:
        with open(args.floor_cert_file) as fh:
            floor_cert = DelegationCert.from_json(fh.read().strip())
        with open(args.floor_channel_cert_file) as fh:
            floor_channel_cert = DelegationCert.from_json(fh.read().strip())

    if args.mode == "serve-file":
        if not args.file:
            parser.error("--mode serve-file requires --file")
        handler = file_handler(args.file, args.content_type)
    else:
        handler = echo_handler

    node = NodeServer(
        args.org, args.root_pub, key, cert, handler,
        floor_key=floor_key, floor_cert=floor_cert,
        floor_channel_cert=floor_channel_cert,
        listen_host=args.listen_host, listen_port=args.listen_port,
        floor_url=args.floor, peer_relay_urls=tuple(args.peer_relay),
        registry_url=args.registry, announce_addrs=tuple(args.announce_addr),
        announce_relay_url=args.announce_relay_url, announce_ttl=args.announce_ttl,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
    )
    asyncio.run(node.run_forever())


if __name__ == "__main__":
    main()
