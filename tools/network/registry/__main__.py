"""Run the registry service: ``python -m tools.network.registry``."""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


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
    args = parser.parse_args()
    app = create_app(args.db, base_url=args.base_url)
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
    )


if __name__ == "__main__":
    main()
