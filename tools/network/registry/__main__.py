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
        default="https://auto.network",
        help="public URL prefix for issued share links",
    )
    args = parser.parse_args()
    app = create_app(args.db, base_url=args.base_url)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
