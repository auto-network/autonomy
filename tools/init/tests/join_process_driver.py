"""Subprocess driver for the restart-safe invite-join acceptance test.

This deliberately enters through ``migrate_on_mount``: it is the exact
data-volume path the Docker entrypoint invokes, rather than the checkout-root
test helper.  The injected transport calls the real claim service in another
rooted org store; B6 replaces that seam with the real relay connector.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path

from tools.data_paths import REFUSE_REAL_DATA_FALLBACK_ENV, STORE_MANIFEST
from tools.network.invitation import decode_invitation


@contextmanager
def _remote_orgs(root: Path, slug: str):
    """Temporarily point the real claim service at the inviting node."""
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    saved_orgs = os.environ.get("AUTONOMY_ORGS_DIR")
    saved_graph_org = os.environ.get("GRAPH_ORG")
    os.environ["AUTONOMY_ORGS_DIR"] = str(root)
    os.environ["GRAPH_ORG"] = slug
    try:
        yield
    finally:
        GraphDB.close_all_pooled()
        if saved_orgs is None:
            os.environ.pop("AUTONOMY_ORGS_DIR", None)
        else:
            os.environ["AUTONOMY_ORGS_DIR"] = saved_orgs
        if saved_graph_org is None:
            os.environ.pop("GRAPH_ORG", None)
        else:
            os.environ["GRAPH_ORG"] = saved_graph_org


class DirectClaimTransport:
    """The B4 transport seam, backed by the production claim service."""

    def __init__(self, invitation, *, orgs_root: Path, slug: str):
        self.invitation = invitation
        if invitation.channel_token == invitation.claim_token:
            raise AssertionError("test transport requires distinct token domains")
        self.orgs_root = orgs_root
        self.slug = slug
        self.wall_clock_ms = None

    def request(self, payload: dict) -> dict:
        from tools.dashboard import claim_service

        with _remote_orgs(self.orgs_root, self.slug):
            real_time = claim_service.time.time
            if self.wall_clock_ms is not None:
                claim_service.time.time = lambda: self.wall_clock_ms / 1000
            operation = payload.get("op")
            try:
                if operation == "context":
                    reply = claim_service.context(
                        self.slug, self.invitation.invite_ref
                    )
                    if reply.get("status") == "ok":
                        # The connector adds the invitation anchor to the
                        # transport-neutral service response.
                        reply = {
                            **reply,
                            "org": self.invitation.org,
                            "root_pub": self.invitation.root_pub,
                        }
                elif operation == "submit":
                    from tools.network.ledger import Event

                    event = Event.from_json(payload["event"])
                    if event.payload.get("token") != self.invitation.claim_token:
                        raise AssertionError(
                            "claim submit omitted the ledger invitation bearer"
                        )
                    if event.payload.get("token") == self.invitation.channel_token:
                        raise AssertionError(
                            "claim submit carried the relay channel credential"
                        )
                    reply = claim_service.submit(self.slug, payload["event"])
                elif operation == "status":
                    reply = claim_service.status(
                        self.slug,
                        self.invitation.invite_ref,
                        payload["persona_pub"],
                    )
                else:
                    raise AssertionError(
                        f"unexpected join operation: {operation!r}"
                    )
            finally:
                claim_service.time.time = real_time
        return {"v": 1, **reply}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--volume", required=True, type=Path)
    parser.add_argument("--remote-orgs", required=True, type=Path)
    parser.add_argument("--remote-slug", required=True)
    parser.add_argument("--invite", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--wall-clock-ms", type=int)
    args = parser.parse_args()

    invitation = decode_invitation(args.invite)
    os.environ[REFUSE_REAL_DATA_FALLBACK_ENV] = "1"
    os.environ["AUTONOMY_INVITE"] = args.invite
    os.environ["AUTONOMY_PERSONAL_PASSWORD_FILE"] = str(args.password_file)
    os.environ.pop("AUTONOMY_FIRST_ORG", None)
    for store in STORE_MANIFEST:
        if store.env:
            os.environ[store.env] = str(args.volume / store.relative)

    from tools.dashboard.dao import pending_joins
    from tools.portability import migrate_on_mount

    transport = DirectClaimTransport(
        invitation,
        orgs_root=args.remote_orgs,
        slug=args.remote_slug,
    )
    transport.wall_clock_ms = args.wall_clock_ms
    report = migrate_on_mount(
        args.volume,
        tls=False,
        join_transport=transport,
    )
    print(json.dumps({
        "report": report,
        "pending": pending_joins.list_pending(),
        "org_dbs": sorted(
            path.name for path in (args.volume / "orgs").glob("*.db")
        ),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
