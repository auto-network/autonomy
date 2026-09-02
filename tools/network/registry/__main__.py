"""Run the registry service: ``python -m tools.network.registry``."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import uvicorn

from .app import create_app


_GIT_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_UNKNOWN_BUILD = {"commit": "unknown", "dirty": None, "built_at": None}
FORWARDED_ALLOW_IPS = "127.0.0.1,::1"


def _load_build_info(path: str | None) -> dict:
    """Read the deploy-written provenance stamp without trusting its shape.

    Provenance is diagnostic, never authority and never a compatibility gate.
    A missing or malformed stamp is reported explicitly as ``unknown`` rather
    than preventing the registry from serving existing links.
    """
    if path is None:
        return dict(_UNKNOWN_BUILD)
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_UNKNOWN_BUILD)
    if (
        not isinstance(value, dict)
        or set(value) != {"commit", "dirty", "built_at"}
        or not isinstance(value.get("commit"), str)
        or _GIT_COMMIT_RE.fullmatch(value["commit"]) is None
        or type(value.get("dirty")) is not bool
        or not isinstance(value.get("built_at"), str)
        or not value["built_at"]
        or len(value["built_at"]) > 64
    ):
        return dict(_UNKNOWN_BUILD)
    return value


def _load_turn_issuer():
    """Use the optional TURN-deploy credential without gating the Registry."""
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credential_dir:
        return None
    path = Path(credential_dir) / "turn-rest-secrets"
    if not path.is_file():
        return None
    from .turn_credentials import TurnCredentialIssuer

    return TurnCredentialIssuer.from_file(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="auto.network registry v1")
    parser.add_argument("--db", default="registry.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8477)
    parser.add_argument(
        "--base-url",
        default="https://relay.auto.network",
        help="public URL prefix for issued share links",
    )
    parser.add_argument(
        "--version-file",
        help="deploy-written JSON provenance stamp (diagnostic only)",
    )
    parser.add_argument(
        "--stream-ingress-port",
        type=int,
        help="enable the raw-stream (tls-stream/1) TCP ingress on this port. "
             "In production this is the serve floating IP's :443 — the ingress "
             "is the public serve edge and sees the client's native source IP.",
    )
    parser.add_argument(
        "--stream-ingress-host",
        default="127.0.0.1",
        help="bind address for the raw-stream ingress (the serve floating IP "
             "in production; loopback for tests)",
    )
    parser.add_argument(
        "--stream-idle-timeout",
        type=float,
        help="idle seconds before a raw stream is reset (default 600)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        help="enable the PRIVATE bounded-cardinality metrics exposition on "
             "this loopback port (auto-albp6.9). Never fronted publicly.",
    )
    parser.add_argument(
        "--metrics-host",
        default="127.0.0.1",
        help="bind address for the private metrics listener",
    )
    parser.add_argument(
        "--abuse-exempt-source",
        action="append",
        default=[],
        metavar="IP",
        help="a source address whose traffic bypasses ALL relay abuse "
             "admission and byte limits (the per-identity override; use for a "
             "trusted/premium identity or a controlled stress test). Repeatable. "
             "Off by default; every other source stays fully limited.",
    )
    args = parser.parse_args()
    app = create_app(
        args.db,
        base_url=args.base_url,
        build_info=_load_build_info(args.version_file),
        turn_issuer=_load_turn_issuer(),
        stream_ingress_port=args.stream_ingress_port,
        stream_ingress_host=args.stream_ingress_host,
        stream_idle_timeout=args.stream_idle_timeout,
        metrics_port=args.metrics_port,
        metrics_host=args.metrics_host,
        abuse_exempt_sources=frozenset(args.abuse_exempt_source),
    )
    # Link tokens are bearer credentials and are part of the public route.
    # Uvicorn's HTTP access logger records the full path, while its WebSocket
    # protocol records accepted paths through uvicorn.error at INFO.  Disable
    # both rather than retaining a source-address-to-credential browsing log.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
        # Caddy is the only public listener. Trust forwarded client
        # addresses exclusively from its loopback hop; a direct/non-loopback
        # caller cannot choose the abuse limiter's source key.
        proxy_headers=True,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
    )


if __name__ == "__main__":
    main()
