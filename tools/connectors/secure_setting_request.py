#!/usr/bin/env python3
"""Request an operator-provisioned encrypted Setting through approvals."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


DEFAULT_DASHBOARD = os.environ.get("AUTONOMY_DASHBOARD", "https://localhost:8080")


def _request_json(url: str, body: dict[str, Any] | None = None,
                  timeout: float = 70) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(url, data=data,
                             method="POST" if body is not None else "GET",
                             headers={"Content-Type": "application/json"})
    # Dashboard uses the host's local TLS endpoint. This CLI is intended for
    # that trusted local boundary; urllib otherwise rejects the local cert.
    import ssl
    context = ssl._create_unverified_context()
    try:
        with urlrequest.urlopen(req, timeout=timeout, context=context) as response:
            return json.loads(response.read())
    except urlerror.HTTPError as exc:
        payload = json.loads(exc.read() or b"{}")
        raise RuntimeError(payload.get("error") or str(exc)) from exc


def request_secure_setting(*, session: str, target_key: str, origin: str,
                           schema: dict[str, Any], title: str,
                           description: str, org: str,
                           dashboard: str = DEFAULT_DASHBOARD,
                           wait: bool = True) -> dict[str, Any]:
    created = _request_json(f"{dashboard.rstrip('/')}/api/approvals", {
        "kind": "secure_setting",
        "session": session,
        "request": {
            "target_key": target_key,
            "origin": origin,
            "schema": schema,
            "title": title,
            "description": description,
            "org": org,
        },
    })
    approval_id = created.get("id")
    if not approval_id or not wait:
        return {"approval_id": approval_id, **created}
    while True:
        result = _request_json(
            f"{dashboard.rstrip('/')}/api/approvals/{approval_id}?wait=55",
            timeout=65,
        )
        decision = result.get("result")
        if decision is None:
            continue
        if not decision.get("approved"):
            return {"ok": False, "approval_id": approval_id,
                    "status": "declined"}
        execution = decision.get("execution") or {}
        return {"approval_id": approval_id, **execution}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_key")
    parser.add_argument("origin")
    parser.add_argument("--field", action="append", default=[],
                        help="FORM_NAME=DICT_KEY; repeat for each secure field")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--org", default=os.environ.get("GRAPH_ORG", "personal"))
    parser.add_argument("--session", default=os.environ.get("AUTONOMY_SESSION", ""))
    parser.add_argument("--dashboard", default=DEFAULT_DASHBOARD)
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()
    if not args.session:
        parser.error("AUTONOMY_SESSION or --session is required")
    schema: dict[str, Any] = {}
    for item in args.field:
        if "=" not in item:
            parser.error(f"invalid --field {item!r}; expected FORM_NAME=DICT_KEY")
        form_name, dict_key = item.split("=", 1)
        schema[form_name] = dict_key
    if not schema:
        parser.error("at least one --field is required")
    result = request_secure_setting(
        session=args.session, target_key=args.target_key, origin=args.origin,
        schema=schema, title=args.title, description=args.description,
        org=args.org, dashboard=args.dashboard, wait=not args.no_wait)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
