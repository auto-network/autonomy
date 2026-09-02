"""Private, bounded-cardinality relay telemetry (auto-albp6.9).

A separate internal listener exposes a Prometheus text-exposition
``/metrics`` for operations and benchmarks. Two rules are load-bearing and
both are tested:

* **Bounded cardinality.** The only label values ever emitted are the
  organization id and a small fixed set of reason enums. Tokens, hostnames,
  reservation ids, personas, and source addresses are NEVER labels — ten
  thousand links add zero series. A per-org series set is bounded by the
  number of paying organizations, which is operational, not attacker-driven.
* **No public reach.** This binds its own loopback listener, distinct from
  the public app; the public Uvicorn app exposes no metrics route.

Gauges are computed at scrape time directly from live hub / host-routes
state, so a gauge can never drift from reality through a missed hook
(acceptance: gauges equal direct state under setup and teardown). Counters
are monotonic and advanced at the event site.

The exposition is hand-rolled (no new dependency): the format is stable and
trivial, and hand-rolling keeps the cardinality discipline explicit rather
than hidden behind a client library's label API.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable

#: Reason enums are the ONLY free-form label values besides org, and they are
#: drawn from these fixed vocabularies — validated on write so a stray value
#: can never widen cardinality.
STREAM_REFUSE_REASONS = frozenset({
    "no_sni", "parse_error", "unrouted", "not_capable", "admission_denied",
    "stream_cap", "open_refused", "open_timeout",
})
LEASE_EVENTS = frozenset({"register", "renew", "renew_all", "release", "expire"})
DNS01_RESULTS = frozenset({"ok", "refused"})
DNS_RCODES = frozenset({"noerror", "nxdomain", "refused", "servfail", "notimp",
                        "formerr", "badvers"})

_ORG_RE = re.compile(r"^[0-9a-fA-F-]{1,64}\Z")


def _san_org(org: str) -> str:
    """Orgs are UUIDs; anything else collapses to 'other' so a malformed or
    hostile org string can never inject label text or widen cardinality."""
    return org if isinstance(org, str) and _ORG_RE.match(org) else "other"


class RegistryMetrics:
    """One per registry process. Counters live here; gauges are pulled from
    the live objects registered via :meth:`bind_state`."""

    def __init__(self) -> None:
        # counter_name -> Counter over a label tuple (org, *enums)
        self._stream_opens: Counter = Counter()      # (org,)
        self._stream_closes: Counter = Counter()     # (org, reason) orderly enums
        self._stream_refusals: Counter = Counter()   # (reason,) pre-org-resolve
        self._stream_bytes: Counter = Counter()      # (org, direction)
        self._lease_events: Counter = Counter()      # (org, event)
        self._dns01_ops: Counter = Counter()         # (result,)
        self._dns_queries: Counter = Counter()       # (rcode,)
        self._gauges: list[tuple[str, str, Callable[[], dict]]] = []

    # -- counter event sites (cheap, monotonic) ----------------------------

    def stream_opened(self, org: str) -> None:
        self._stream_opens[(_san_org(org),)] += 1

    def stream_closed(self, org: str, reason: str) -> None:
        reason = reason if reason in {"orderly", "reset", "timeout",
                                      "byte_budget"} else "other"
        self._stream_closes[(_san_org(org), reason)] += 1

    def stream_refused(self, reason: str) -> None:
        # Pre-routing refusals have no resolved org; count by reason only.
        reason = reason if reason in STREAM_REFUSE_REASONS else "other"
        self._stream_refusals[(reason,)] += 1

    def stream_bytes(self, org: str, direction: str, n: int) -> None:
        if direction not in ("in", "out") or n <= 0:
            return
        self._stream_bytes[(_san_org(org), direction)] += n

    def lease_event(self, org: str, event: str) -> None:
        if event not in LEASE_EVENTS:
            return
        self._lease_events[(_san_org(org), event)] += 1

    def dns01_op(self, result: str) -> None:
        result = "ok" if result == "ok" else "refused"
        self._dns01_ops[(result,)] += 1

    def dns_query(self, rcode: str) -> None:
        rcode = rcode if rcode in DNS_RCODES else "other"
        self._dns_queries[(rcode,)] += 1

    # -- scrape-time gauges (read live state, cannot drift) -----------------

    def bind_state(self, *, hub=None, host_routes=None) -> None:
        """Register the live objects the gauge collectors read at scrape."""
        if hub is not None:
            self._gauges.append((
                "relay_tunnels", "Live tunnels, per organization.",
                lambda: self._tunnel_gauges(hub),
            ))
        if host_routes is not None:
            self._gauges.append((
                "relay_active_leases",
                "Live hostname leases, per organization.",
                lambda: self._lease_gauge(host_routes),
            ))

    @staticmethod
    def _tunnel_gauges(hub) -> dict:
        # Emits three related gauges from one hub walk; returned as a nested
        # dict {gauge_name: {(labels): value}}.
        tunnels: Counter = Counter()
        channels: Counter = Counter()
        streams: Counter = Counter()
        for org, slots in hub._tunnels.items():
            o = _san_org(org)
            tunnels[(o,)] += len(slots)
            for tunnel in slots.values():
                channels[(o,)] += len(getattr(tunnel, "channels", ()))
                streams[(o,)] += len(getattr(tunnel, "raw_streams", ()))
        return {
            "relay_tunnels": dict(tunnels),
            "relay_viewer_channels": dict(channels),
            "relay_raw_streams": dict(streams),
        }

    @staticmethod
    def _lease_gauge(host_routes) -> dict:
        leases: Counter = Counter()
        for lease in host_routes._leases.values():
            leases[(_san_org(getattr(lease.tunnel, "org", "other")),)] += 1
        return {"relay_active_leases": dict(leases)}

    # -- exposition ---------------------------------------------------------

    def render(self) -> str:
        lines: list[str] = []

        def emit(name: str, help_text: str, mtype: str,
                 series: dict, label_names: tuple[str, ...]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            for labels, value in sorted(series.items()):
                if label_names:
                    inner = ",".join(
                        f'{n}="{v}"' for n, v in zip(label_names, labels)
                    )
                    lines.append(f"{name}{{{inner}}} {value}")
                else:
                    lines.append(f"{name} {value}")

        emit("relay_stream_opens_total",
             "Raw streams successfully opened.", "counter",
             self._stream_opens, ("org",))
        emit("relay_stream_closes_total",
             "Raw streams closed, by orderly/reset/timeout/byte_budget.",
             "counter", self._stream_closes, ("org", "reason"))
        emit("relay_stream_refusals_total",
             "Ingress connections refused before routing, by reason.",
             "counter", self._stream_refusals, ("reason",))
        emit("relay_stream_bytes_total",
             "Raw-stream bytes forwarded, by direction.", "counter",
             self._stream_bytes, ("org", "direction"))
        emit("relay_lease_events_total",
             "Hostname-lease control events, by type.", "counter",
             self._lease_events, ("org", "event"))
        emit("relay_dns01_ops_total",
             "DNS-01 control ops, by result.", "counter",
             self._dns01_ops, ("result",))
        emit("relay_dns_queries_total",
             "Authoritative DNS answers, by rcode.", "counter",
             self._dns_queries, ("rcode",))
        # Scrape-time gauges.
        gauge_help = {
            "relay_tunnels": ("Live tunnels, per organization.", ("org",)),
            "relay_viewer_channels":
                ("Live viewer channels, per organization.", ("org",)),
            "relay_raw_streams":
                ("Live raw streams, per organization.", ("org",)),
            "relay_active_leases":
                ("Live hostname leases, per organization.", ("org",)),
        }
        rendered_gauges: dict = {}
        for _name, _help, collector in self._gauges:
            rendered_gauges.update(collector())
        for gname, (ghelp, glabels) in gauge_help.items():
            if gname in rendered_gauges:
                emit(gname, ghelp, "gauge", rendered_gauges[gname], glabels)
        return "\n".join(lines) + "\n"


async def start_metrics_listener(host: str, port: int, metrics: RegistryMetrics):
    """A minimal loopback HTTP/1.1 responder serving GET /metrics only —
    its own listener, never the public app. Stdlib asyncio, no framework, so
    it shares no route table, middleware, or auth surface with the public
    Uvicorn app (defence in depth: a metrics route cannot leak onto :443)."""
    import asyncio

    async def handle(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            line = request.decode("latin-1", "replace").strip()
            # Drain headers (bounded) so keep-alive clients don't wedge.
            while True:
                h = await asyncio.wait_for(reader.readline(), 5)
                if h in (b"\r\n", b"\n", b""):
                    break
            if line.startswith("GET /metrics"):
                body = metrics.render().encode("utf-8")
                status = b"200 OK"
                ctype = b"text/plain; version=0.0.4; charset=utf-8"
            else:
                body, status, ctype = b"", b"404 Not Found", b"text/plain"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: " + ctype + b"\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            import contextlib
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(handle, host, port)
    return server
