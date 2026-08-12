#!/usr/bin/env python3
"""Post-deploy smoke test: is a real link actually working again?

``/healthz`` proves the process started. It does not prove a link resolves,
that the handshake still verifies, or that a guest gets bytes -- and those
are what a deploy breaks. This walks the same path a browser walks, in the
same order, and fails loudly at whichever rung stops working.

    deploy/smoke.py https://relay.auto.network [--link https://.../l/<token>]

Without ``--link`` it runs the unauthenticated rungs only (health, shell,
bootloader asset, CSP) and says so. With one, it also opens the channel,
runs the X25519 handshake pinned to the envelope's org root key, and asks
for the artifact header -- the full guest path, end to end.

Exit status is the point: 0 means the rungs that ran all passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import urlparse

import httpx

REPO_ROOT = __file__.rsplit("/tools/", 1)[0]
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.network.relaykit.viewer import ViewerChannel  # noqa: E402

#: Directives whose absence is a real regression, not a style change.
#: Frame isolation is the iframe's own sandbox attribute, NOT a CSP
#: directive -- do not add one here expecting to find it.
#:
#: base-uri about: is the load-bearing one, and it fails silently: under
#: 'none' the browser ignores the composed document's <base> with no
#: console error and no visible difference from omitting it, and every
#: in-page #fragment link navigates the frame away instead of scrolling.
REQUIRED_CSP = ("default-src 'none'", "base-uri about:", "frame-ancestors 'none'")


class SmokeFailure(Exception):
    pass


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        raise SmokeFailure(name)


def http_rungs(base: str) -> None:
    with httpx.Client(base_url=base.rstrip("/"), timeout=15.0,
                      follow_redirects=True) as client:
        health = client.get("/healthz")
        _check("healthz", health.status_code == 200, f"{health.status_code}")

        asset = client.get("/l-assets/autonet.js")
        _check("bootloader asset serves", asset.status_code == 200,
               f"{len(asset.content)} bytes")
        # A deploy that shipped a truncated or wrong file still returns 200.
        _check("bootloader is the real one",
               b"performHandshake" in asset.content
               and b"ChannelBroker" in asset.content)
        _check("bootloader is uncacheable",
               asset.headers.get("cache-control") == "no-store",
               asset.headers.get("cache-control", "<missing>"))

        # ONE byte sequence for every token; only the STATUS differs, and it
        # mirrors the envelope endpoint's liveness rule so the shell opens no
        # oracle the envelope does not already. An unknown token is a 404
        # that still carries the whole shell -- asserting 200 here would be
        # asserting a leak.
        shell = client.get("/l/not-a-real-token")
        _check("unknown token is 404, not an oracle", shell.status_code == 404,
               f"{shell.status_code}")
        _check("shell bytes served anyway",
               b"/l-assets/autonet.js" in shell.content,
               f"{len(shell.content)} bytes")
        csp = shell.headers.get("content-security-policy", "")
        for directive in REQUIRED_CSP:
            _check(f"CSP carries {directive!r}", directive in csp)


async def channel_rungs(base: str, token: str) -> None:
    with httpx.Client(base_url=base.rstrip("/"), timeout=15.0) as client:
        response = client.get(f"/v1/links/{token}/envelope")
        _check("link envelope resolves", response.status_code == 200,
               f"{response.status_code}")
        envelope = response.json()

    scheme = "wss" if urlparse(base).scheme == "https" else "ws"
    relay = f"{scheme}://{urlparse(base).netloc}"

    # The handshake is pinned to the org root key the envelope names, exactly
    # as the browser pins it. A relay serving someone else's content would
    # fail here rather than render.
    channel = await ViewerChannel.connect(
        relay, token, root_pub=envelope["root_pub"], org=envelope["org"],
    )
    async with channel:
        _check("handshake verifies against the org root key", True)
        await channel.send_message(json.dumps({"v": 1, "op": "head"}).encode())
        raw = await channel.recv_message()
        header = json.loads(raw.split(b"\n", 1)[0])
        _check("artifact header served", header.get("status") == "ok",
               json.dumps(header)[:120])
        size = header.get("serialized_size", 0)
        _check("artifact is non-empty", size > 0, f"{size} bytes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="e.g. https://relay.auto.network")
    parser.add_argument("--link", help="a real link URL, to test the guest path")
    args = parser.parse_args()

    print(f"smoke: {args.base}")
    try:
        http_rungs(args.base)
        if args.link:
            token = args.link.rstrip("/").rsplit("/", 1)[-1].split("#")[0]
            print(f"  link: …{token[-8:]}")
            asyncio.run(channel_rungs(args.base, token))
        else:
            print("  --   guest path SKIPPED (no --link given): this run did "
                  "NOT prove a link works")
    except SmokeFailure as failure:
        print(f"\nsmoke FAILED at: {failure}", file=sys.stderr)
        return 1
    except Exception as error:                      # noqa: BLE001
        print(f"\nsmoke ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("smoke: all rungs passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
