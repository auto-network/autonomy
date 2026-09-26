#!/usr/bin/env python3
"""Read-only gate probes using an existing private browser state export.

Never prints cookie values or OAuth redirect URLs. Does not log in, create
registrations, or publish services. The operator must complete real logins first.
"""

import argparse
import json
import stat
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(origin, path="/", headers=None, data=None):
    opener = build_opener(NoRedirect())
    try:
        response = opener.open(Request(origin + path, headers=headers or {}, data=data), timeout=20)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read(8192)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-state", type=Path, required=True)
    parser.add_argument("--host-a", required=True)
    parser.add_argument("--host-b", required=True)
    parser.add_argument("--host-c", required=True)
    args = parser.parse_args()
    metadata = args.browser_state.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        parser.error("browser state must be owner-only")
    state = json.loads(args.browser_state.read_text())
    cookies = {c["domain"]: c for c in state["cookies"] if c["name"] == "__Host-autonomy_service"}
    results = []

    def check(name, passed, status=None):
        results.append({"check": name, "passed": bool(passed), "status": status})

    for host, marker in ((args.host_a, "a"), (args.host_b, "b"), (args.host_c, "a")):
        origin = "https://" + host
        status, headers, body = request(origin)
        check(f"{host}: anonymous denied", status == 302 and b"authenticated-service:" not in body, status)
        check(f"{host}: safe login redirect", headers.get("Location", "").startswith("/oauth2/start?"))
        status, _, body = request(origin, headers={
            "Authorization": "Bearer forged", "X-Auth-Request-User": "forged",
            "Remote-User": "forged", "X-Forwarded-User": "forged",
        })
        check(f"{host}: forged identity denied", status == 302 and b"authenticated-service:" not in body, status)
        status, _, body = request(origin, "/oauth2/callback", data=b"code=invalid&state=invalid",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        # OAuth2 Proxy returns 500 for structurally malformed state, 403 for
        # well-formed state without a matching CSRF cookie. Both must deny.
        check(f"{host}: malformed callback denied", status == 500 and b"authenticated-service:" not in body, status)
        cookie = cookies.get(host)
        check(f"{host}: real browser session present", cookie is not None)
        if not cookie:
            continue
        check(f"{host}: cookie flags", cookie["secure"] and cookie["httpOnly"]
              and cookie["sameSite"] == "Lax" and cookie["path"] == "/")
        status, _, body = request(origin, headers={"Cookie": cookie["name"] + "=" + cookie["value"]})
        check(f"{host}: authenticated app", status == 200 and body == f"authenticated-service:{marker}\n".encode(), status)
        status, _, body = request(origin, headers={"Cookie": cookie["name"] + "=tampered"})
        check(f"{host}: tampered session denied", status == 302 and b"authenticated-service:" not in body, status)

    if args.host_a in cookies:
        cookie = cookies[args.host_a]
        status, _, body = request("https://" + args.host_b,
                                  headers={"Cookie": cookie["name"] + "=" + cookie["value"]})
        check("A session forcibly copied to independent B denied", status == 302 and b"authenticated-service:" not in body, status)
    status, headers, _ = request("https://" + args.host_a, "/oauth2/start")
    authorize = parse_qs(urlsplit(headers.get("Location", "")).query)
    check("Login uses S256 PKCE and form_post", status == 302
          and authorize.get("code_challenge_method") == ["S256"]
          and authorize.get("response_mode") == ["form_post"])
    if "state" in authorize:
        payload = urlencode({"state": authorize["state"][0], "code": "invalid-test-code"}).encode()
        status, _, body = request("https://" + args.host_b, "/oauth2/callback", data=payload,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        check("A login transaction callback sent to B without its browser binding denied",
              status == 403 and b"authenticated-service:" not in body, status)
    print(json.dumps({"checks": results, "passed": all(r["passed"] for r in results)}, indent=2))
    raise SystemExit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
