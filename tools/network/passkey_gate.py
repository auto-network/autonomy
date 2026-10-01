"""The dashboard's passkey gate: the forward-auth helper beside the Service gateway.

One small process, run from the node image in the gateway's network namespace
(``render_helper_service``), that the gateway consults for every request to
the dashboard's relay route and to every session Service set to Personal
(``service_gateway.render_gate``: ``forward_auth`` on ``/oauth2/auth``, the
rest of ``/oauth2/*`` proxied here). The dashboard process, or the Service,
sees nothing until this helper has said yes. One relying party covers them
all: the operator's own suffix (``gate_rp_id``; operator decision D12,
auto-z98nc), so one enrolled passkey signs in at each gated hostname, each
with its own cookie.

Decided by the operator on 2026-09-27 (graph://c9d72ea4-feb, option O2d):
our own helper on py_webauthn, the library the dashboard already uses for its
identity passkey, not a third-party identity provider. What it does:

* ``GET /oauth2/auth`` — the forward-auth check: a valid gate cookie answers
  200, anything else 401 (the gateway then redirects to ``/oauth2/start``).
* ``GET /oauth2/start`` — the login page: one WebAuthn assertion against a
  credential enrolled for the shared relying party (or, from before
  services, for this exact hostname). ``POST /oauth2/login/options`` and
  ``POST /oauth2/login/verify`` are its two calls.
* ``GET /oauth2/enroll?token=…`` — the enrollment page, reachable only with
  the one-time token the dashboard minted (sha256 + expiry in the gate
  record). ``POST /oauth2/enroll/options`` and ``POST /oauth2/enroll/verify``
  register exactly one passkey; the verified credential is handed to the
  dashboard over its plain listener (``/api/network/remote-access/gate/
  registered``, authenticated with the helper secret), which records it,
  closes enrollment and rewrites the gate record.

State lives with the dashboard (a Settings row); this process reads the gate
record the dashboard materializes into its runtime directory on every request,
so a re-opened enrollment, a revoked passkey or a new sign count reach it with
no restart. The cookie is an HMAC over ``expiry.nonce`` under the machine's
cookie key, 12 hours, ``__Host-`` scoped, HttpOnly, SameSite=Lax. Nothing here
imports the dashboard package: the helper holds the cookie key, the helper
secret and public keys, and nothing else.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from tools.network import clock

HELPER_ID = "dashboard-passkey"


def gate_rp_id(hostname: str) -> str:
    """The relying party a gate passkey is registered for: the operator's own
    suffix, so one enrolled passkey verifies at the dashboard route AND at
    every Personal service published beside it (auto-z98nc, D12).
    ``dashboard.<label>.serve.auto.network`` -> ``<label>.serve.auto.network``;
    ``themes.autonomy.example.com`` -> ``autonomy.example.com``. WebAuthn lets
    the RP ID be any registrable-domain suffix of the origin's host; the
    persona label (or the verified custom zone) is the tightest one the
    operator owns, never a public suffix. A host too short to carry a label
    above such a suffix is its own relying party."""
    host = (hostname or "").strip().lower().rstrip(".")
    labels = host.split(".")
    if len(labels) >= 4:
        suffix = ".".join(labels[1:])
        # Never the relay's shared base: that is every operator's suffix,
        # not this one's.
        if suffix != RELAY_BASE:
            return suffix
    return host


#: The relay's shared base under which every persona label lives.
RELAY_BASE = "serve.auto.network"


def _rp_covers(rp_id: str, host: str) -> bool:
    """Whether a credential registered for *rp_id* may be asserted at *host*
    (the host equals the RP ID or is a subdomain of it)."""
    rp_id, host = (rp_id or "").lower(), (host or "").lower()
    return bool(rp_id) and (host == rp_id or host.endswith("." + rp_id))
COOKIE_NAME = "__Host-autonomy-gate"
#: The two refusal windows live in tools/network/clock.py with every other
#: gate: clock.PASSKEY_GATE_SESSION_TTL_S (the cookie) and
#: clock.PASSKEY_GATE_CHALLENGE_TTL_S (a pending WebAuthn challenge).
#: The unauthenticated options calls can create pending challenges; the
#: bound is memory (a few hundred bytes each), so it is generous rather than
#: a lever for evicting a real visitor's ceremony.
PENDING_MAX = 4096
RP_NAME = "Autonomy"
#: The dashboard route this helper reports and calls back on.
REGISTERED_PATH = "/api/network/remote-access/gate/registered"
SIGN_COUNT_PATH = "/api/network/remote-access/gate/sign-count"

#: Compose service the gateway supervisor renders for this helper: the node's
#: own image and code, read-only, no ports, the gateway's network namespace.
_LABEL = "autonomy.auth-config"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _now() -> float:
    return clock.now_s()


class GateRuntime:
    """The helper's view of its runtime directory, read per request."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def record(self) -> dict:
        try:
            data = json.loads((self.directory / "gate.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def cookie_key(self) -> bytes:
        return bytes.fromhex((self.directory / "cookie-secret").read_text().strip())

    def helper_secret(self) -> str:
        return (self.directory / "helper-secret").read_text().strip()


# ── gate cookie ─────────────────────────────────────────────────────────

def mint_cookie(key: bytes, *, now: float | None = None,
                ttl: float = clock.PASSKEY_GATE_SESSION_TTL_S) -> str:
    expiry = int((now if now is not None else _now()) + ttl)
    nonce = secrets.token_hex(16)
    body = f"{expiry}.{nonce}"
    return f"{body}.{hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}"


def cookie_valid(key: bytes, value: str | None, *, now: float | None = None) -> bool:
    if not value or value.count(".") != 2:
        return False
    expiry, nonce, signature = value.split(".")
    if not expiry.isdigit() or len(nonce) != 32 or len(signature) != 64:
        return False
    expected = hmac.new(key, f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    return int(expiry) > (now if now is not None else _now())


def _set_cookie(response: Response, key: bytes) -> None:
    response.set_cookie(
        COOKIE_NAME, mint_cookie(key), max_age=clock.PASSKEY_GATE_SESSION_TTL_S, path="/",
        secure=True, httponly=True, samesite="lax",
    )


# ── enrollment token ────────────────────────────────────────────────────

def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def enrollment_open(record: dict, token: str | None, *, now: float | None = None) -> bool:
    """True when the record's enrollment is open, unexpired, and *token*
    is the one it was opened with."""
    enrollment = record.get("enrollment")
    if not isinstance(enrollment, dict) or not enrollment.get("open"):
        return False
    if not isinstance(token, str) or not token:
        return False
    expires_at = enrollment.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= (now if now is not None else _now()):
        return False
    expected = enrollment.get("token_sha256")
    return isinstance(expected, str) and hmac.compare_digest(expected, token_sha256(token))


# ── the application ─────────────────────────────────────────────────────

class GateApp:
    def __init__(self, runtime: GateRuntime, *, post_dashboard=None) -> None:
        self.runtime = runtime
        self._pending_login: dict[str, dict] = {}
        self._pending_enroll: dict[str, dict] = {}
        self._post_dashboard = post_dashboard or _post_dashboard

    # -- helpers --

    def _prune(self, store: dict, *, reserve: int = 0) -> None:
        cutoff = _now()
        for key in [k for k, p in store.items() if p["expires"] <= cutoff]:
            store.pop(key, None)
        limit = max(PENDING_MAX - reserve, 0)
        while len(store) > limit:
            store.pop(next(iter(store)))

    @staticmethod
    def _allowed_origins(record: dict) -> list[str]:
        """The origins this gate stands in front of: the dashboard route and
        every Personal service (the projection's ``origins``); a record from
        before services carried one ``origin``."""
        origins = record.get("origins")
        if not isinstance(origins, list) or not origins:
            origins = [record.get("origin")] if record.get("origin") else []
        return [o.rstrip("/") for o in origins if isinstance(o, str) and o]

    @staticmethod
    def _request_host(request: Request) -> str:
        """The hostname the visitor used: the gateway's forwarded host (it
        proxies /oauth2/* and forward_auth to this helper), else the Host."""
        forwarded = request.headers.get("x-forwarded-host") or ""
        host = forwarded.split(",")[0].strip() or request.headers.get("host") or ""
        return host.split(":")[0].strip().lower()

    def _origin_for(self, record: dict, request: Request) -> str | None:
        """The origin a ceremony at this request is bound to: the request's
        own origin when it is one this gate serves, else nothing (a ceremony
        is never accepted for a host the gate does not stand in front of)."""
        host = self._request_host(request)
        origin = f"https://{host}" if host else ""
        return origin if origin in self._allowed_origins(record) else None

    @staticmethod
    def _rp_for(record: dict, host: str) -> str | None:
        """The relying party a login at *host* asserts under: the shared
        suffix when a passkey is enrolled for it, else the host itself when a
        passkey was enrolled for that exact name (a dashboard-only enrolment
        from before services), else None (nothing enrolled for this host)."""
        shared = record.get("rp_id") or ""
        registered = {str(row.get("rp_id") or shared) for row in record.get("credentials") or []}
        if shared and _rp_covers(shared, host) and shared in registered:
            return shared
        if host in registered:
            return host
        return None

    def _redirect_target(self, record: dict, rd: str | None) -> str:
        """Only a URL on an origin this gate stands in front of is followed
        after login."""
        if isinstance(rd, str) and _SAFE_URL_RE.match(rd):
            for origin in self._allowed_origins(record):
                if rd.startswith(origin + "/") and not urlsplit(rd).fragment:
                    return rd
        return "/"

    # -- forward-auth --

    async def auth(self, request: Request) -> Response:
        try:
            key = self.runtime.cookie_key()
        except Exception:
            return Response(status_code=503)
        if cookie_valid(key, request.cookies.get(COOKIE_NAME)):
            return Response(status_code=200, headers={"X-Auth-Gate": HELPER_ID})
        return Response(status_code=401)

    # -- login --

    async def start(self, request: Request) -> Response:
        record = self.runtime.record()
        credentials = record.get("credentials") or []
        rd = self._redirect_target(record, request.query_params.get("rd"))
        return _html(_login_page(rd, enrolled=bool(credentials)))

    async def login_options(self, request: Request) -> Response:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement

        record = self.runtime.record()
        if not record.get("rp_id") or not self._allowed_origins(record):
            return JSONResponse({"ok": False, "error": "gate is not configured"}, status_code=503)
        origin = self._origin_for(record, request)
        if origin is None:
            return JSONResponse({"ok": False, "error": "this address is not behind the gate"}, status_code=403)
        rp_id = self._rp_for(record, self._request_host(request))
        if rp_id is None:
            return JSONResponse({"ok": False, "error": (
                "no passkey is enrolled for this address; open enrollment from the "
                "dashboard on the machine itself")}, status_code=409)
        # Discoverable login: the passkeys are resident, so the browser offers
        # them itself and this public page discloses no credential id.
        options = generate_authentication_options(
            rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
        )
        self._prune(self._pending_login, reserve=1)
        self._pending_login[_b64url(options.challenge)] = {
            "rp_id": rp_id, "origin": origin, "expires": _now() + clock.PASSKEY_GATE_CHALLENGE_TTL_S,
        }
        return JSONResponse({"ok": True, "options": json.loads(options_to_json(options))})

    async def login_verify(self, request: Request) -> Response:
        from webauthn import verify_authentication_response
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse

        body = await _json_body(request)
        credential = body.get("credential") if isinstance(body, dict) else None
        if not isinstance(credential, dict):
            return JSONResponse({"ok": False, "error": "body must carry 'credential'"}, status_code=400)
        challenge_key = _challenge_of(credential)
        self._prune(self._pending_login)
        pending = self._pending_login.pop(challenge_key, None) if challenge_key else None
        if pending is None:
            return JSONResponse({"ok": False, "error": (
                "unknown or expired passkey challenge; start the login again")}, status_code=400)
        record = self.runtime.record()
        row = next((r for r in record.get("credentials") or []
                    if r.get("credential_id") == credential.get("rawId")), None)
        # A passkey answers only under the relying party it was registered
        # for: a dashboard-only enrolment never opens a service address.
        if row is None or str(row.get("rp_id") or record.get("rp_id")) != pending["rp_id"]:
            return JSONResponse({"ok": False, "error": "this passkey is not enrolled here"}, status_code=403)
        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=_b64url_decode(challenge_key),
                expected_rp_id=pending["rp_id"],
                expected_origin=pending["origin"],
                credential_public_key=_b64url_decode(row["public_key"]),
                credential_current_sign_count=int(row.get("sign_count") or 0),
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as exc:
            return JSONResponse({"ok": False, "error": f"passkey assertion did not verify: {exc}"},
                                status_code=403)
        except Exception as exc:  # noqa: BLE001 — malformed input is a refusal, not a crash
            return JSONResponse({"ok": False, "error": f"malformed passkey assertion: {exc}"},
                                status_code=400)
        # The new sign count guards against a cloned authenticator; recorded
        # best-effort (a lost update only weakens that one check).
        try:
            self._post_dashboard(record, self.runtime.helper_secret(), SIGN_COUNT_PATH, {
                "credential_id": row["credential_id"], "sign_count": verification.new_sign_count,
            })
        except Exception:
            pass
        response = JSONResponse({"ok": True, "redirect": self._redirect_target(
            record, body.get("rd") if isinstance(body, dict) else None)})
        _set_cookie(response, self.runtime.cookie_key())
        return response

    # -- enrollment --

    async def enroll_page(self, request: Request) -> Response:
        record = self.runtime.record()
        token = request.query_params.get("token")
        if not enrollment_open(record, token):
            return _html(_closed_page(), status_code=403)
        return _html(_enroll_page(token))

    async def enroll_options(self, request: Request) -> Response:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
            ResidentKeyRequirement, UserVerificationRequirement,
        )

        body = await _json_body(request)
        token = body.get("token") if isinstance(body, dict) else None
        record = self.runtime.record()
        if not enrollment_open(record, token):
            return JSONResponse({"ok": False, "error": "enrollment is closed"}, status_code=403)
        rp_id, origin = record.get("rp_id"), self._origin_for(record, request)
        if not rp_id or not self._allowed_origins(record):
            return JSONResponse({"ok": False, "error": "gate is not configured"}, status_code=503)
        if origin is None:
            return JSONResponse({"ok": False, "error": "this address is not behind the gate"}, status_code=403)
        exclude = []
        for row in record.get("credentials") or []:
            try:
                exclude.append(PublicKeyCredentialDescriptor(id=_b64url_decode(row["credential_id"])))
            except Exception:
                continue
        options = generate_registration_options(
            rp_id=rp_id, rp_name=RP_NAME,
            # One user, this dashboard: a stable, non-identifying handle.
            user_id=hashlib.sha256(rp_id.encode()).digest(),
            user_name="dashboard", user_display_name="Autonomy dashboard",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude or None,
        )
        self._prune(self._pending_enroll, reserve=1)
        self._pending_enroll[_b64url(options.challenge)] = {
            "rp_id": rp_id, "origin": origin, "expires": _now() + clock.PASSKEY_GATE_CHALLENGE_TTL_S,
        }
        return JSONResponse({"ok": True, "options": json.loads(options_to_json(options))})

    async def enroll_verify(self, request: Request) -> Response:
        from webauthn import verify_registration_response
        from webauthn.helpers.exceptions import InvalidRegistrationResponse

        body = await _json_body(request)
        token = body.get("token") if isinstance(body, dict) else None
        credential = body.get("credential") if isinstance(body, dict) else None
        record = self.runtime.record()
        if not enrollment_open(record, token):
            return JSONResponse({"ok": False, "error": "enrollment is closed"}, status_code=403)
        if not isinstance(credential, dict):
            return JSONResponse({"ok": False, "error": "body must carry 'credential'"}, status_code=400)
        challenge_key = _challenge_of(credential)
        self._prune(self._pending_enroll)
        pending = self._pending_enroll.pop(challenge_key, None) if challenge_key else None
        if pending is None:
            return JSONResponse({"ok": False, "error": (
                "unknown or expired passkey challenge; start the enrollment again")}, status_code=400)
        try:
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=_b64url_decode(challenge_key),
                expected_rp_id=pending["rp_id"],
                expected_origin=pending["origin"],
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as exc:
            return JSONResponse({"ok": False, "error": f"passkey registration did not verify: {exc}"},
                                status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"malformed passkey registration: {exc}"},
                                status_code=400)
        raw_transports = (credential.get("response") or {}).get("transports") or []
        registered = {
            "token": token,
            "credential_id": _b64url(verification.credential_id),
            "public_key": _b64url(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "transports": [t for t in raw_transports if isinstance(t, str)],
            "rp_id": pending["rp_id"],
        }
        # The dashboard owns the record: it checks the token once more,
        # appends the credential, closes enrollment and rewrites gate.json.
        try:
            reply = self._post_dashboard(record, self.runtime.helper_secret(), REGISTERED_PATH, registered)
        except Exception as exc:  # noqa: BLE001
            _log(f"registration callback to the dashboard failed: {exc}")
            return JSONResponse({"ok": False, "error": f"the dashboard did not record the passkey: {exc}"},
                                status_code=502)
        _log(f"registration callback answered: {reply!r}"[:300])
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            return JSONResponse({"ok": False, "error": (
                (reply or {}).get("error") if isinstance(reply, dict) else None)
                or "the dashboard refused the passkey"}, status_code=502)
        response = JSONResponse({"ok": True, "redirect": "/"})
        _set_cookie(response, self.runtime.cookie_key())
        return response

    def routes(self) -> list[Route]:
        return [
            Route("/oauth2/auth", self.auth, methods=["GET", "HEAD"]),
            Route("/oauth2/start", self.start, methods=["GET"]),
            Route("/oauth2/login/options", self.login_options, methods=["POST"]),
            Route("/oauth2/login/verify", self.login_verify, methods=["POST"]),
            Route("/oauth2/enroll", self.enroll_page, methods=["GET"]),
            Route("/oauth2/enroll/options", self.enroll_options, methods=["POST"]),
            Route("/oauth2/enroll/verify", self.enroll_verify, methods=["POST"]),
        ]


def build_app(runtime_dir: Path | str, *, post_dashboard=None) -> Starlette:
    gate = GateApp(GateRuntime(runtime_dir), post_dashboard=post_dashboard)
    return Starlette(routes=gate.routes())


def _html(body: str, status_code: int = 200) -> HTMLResponse:
    """Every page: no referrer leaves it (the enrollment URL carries the
    token; a same-origin Referer would land in the gateway's log)."""
    return HTMLResponse(body, status_code=status_code, headers={
        "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "X-Frame-Options": "DENY",
    })


async def _json_body(request: Request):
    try:
        return await request.json()
    except Exception:
        return None


def _challenge_of(credential: dict) -> str | None:
    """The challenge a WebAuthn response answered, from its clientDataJSON."""
    try:
        client_data = json.loads(_b64url_decode(credential["response"]["clientDataJSON"]))
        challenge = client_data.get("challenge")
        return challenge if isinstance(challenge, str) else None
    except Exception:
        return None


def _log(message: str) -> None:
    import sys

    print(f"passkey gate: {message}", file=sys.stderr, flush=True)


def _post_dashboard(record: dict, helper_secret: str, path: str, payload: dict) -> dict:
    """One JSON POST to the dashboard's plain listener on the compose network,
    authenticated with the helper secret. Synchronous, five seconds."""
    import urllib.request

    upstream = record.get("dashboard_upstream")
    if not isinstance(upstream, str) or not upstream:
        raise RuntimeError("gate record names no dashboard upstream")
    request = urllib.request.Request(
        f"http://{upstream}{path}", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {helper_secret}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 — fixed http upstream
        return json.loads(response.read().decode("utf-8"))


# ── compose service ──────────────────────────────────────────────────────

def render_helper_service(runtime: Path, revision: str, *, image: str, port: int,
                          app_mount: dict | None = None) -> dict:
    """The compose service that runs this helper beside the gateway: the
    dashboard's own image and, when given, its own code mount, so the helper
    runs exactly the code the dashboard runs; read-only, no ports, no
    capabilities, the gateway's network namespace."""
    volumes = [{"type": "bind", "source": str(runtime), "target": "/run/gate", "read_only": True}]
    if app_mount:
        volumes.append({**app_mount, "target": "/app", "read_only": True})
    return {
        "image": image,
        "profiles": ["service-gateway"],
        "restart": "unless-stopped",
        "user": "1000:1000",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "network_mode": "service:service-gateway",
        "depends_on": {"service-gateway": {"condition": "service_started"}},
        "entrypoint": ["python3", "-m", "tools.network.passkey_gate"],
        "command": ["--runtime", "/run/gate", "--port", str(port)],
        "working_dir": "/app",
        "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "HOME": "/tmp"},
        "tmpfs": ["/tmp"],
        "labels": {_LABEL: revision},
        "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "2"}},
        "volumes": volumes,
    }


# ── pages ────────────────────────────────────────────────────────────────

_STYLE = """
body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:flex;min-height:100vh;align-items:center;justify-content:center}
main{max-width:26rem;padding:2rem;text-align:center}
h1{font-size:1.4rem;margin:0 0 .5rem}
p{color:#b8bcc4;margin:.5rem 0 1.2rem}
button{font:inherit;padding:.7rem 1.4rem;border-radius:.6rem;border:0;background:#4f8cff;color:#fff;cursor:pointer}
button[disabled]{opacity:.5;cursor:default}
.err{color:#ff8a8a;min-height:1.5rem;margin-top:1rem}
"""

_JS_COMMON = """
function b64u(buf){return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');}
function unb64u(s){s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';return Uint8Array.from(atob(s),c=>c.charCodeAt(0));}
function fail(msg){document.getElementById('err').textContent=msg;document.getElementById('go').disabled=false;}
async function post(path,body){var r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});var j=await r.json().catch(function(){return {}});if(!r.ok||!j.ok)throw new Error(j.error||('request failed ('+r.status+')'));return j;}
"""


#: A redirect target is followed only when it is a plain URL: RFC 3986
#: characters, nothing that could close an attribute or a script block.
_SAFE_URL_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")


def _js_string(value: str) -> str:
    """*value* as a JavaScript string literal safe inside a ``<script>``
    block: JSON, with the characters that end a script block or an HTML
    entity escaped as well (``json.dumps`` leaves ``<`` alone, so a value
    containing ``</script>`` would otherwise end the block)."""
    return (json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _page(title: str, body: str, script: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<title>{title}</title><style>{_STYLE}</style></head><body><main>{body}"
        f"<div class=\"err\" id=\"err\"></div></main><script>{_JS_COMMON}{script}</script></body></html>"
    )


def _login_page(rd: str, *, enrolled: bool) -> str:
    if not enrolled:
        return _page("Autonomy", (
            "<h1>No passkey enrolled</h1><p>This address is protected by a passkey that has not "
            "been enrolled yet. Open enrollment from the dashboard on the machine itself, or from "
            "another machine of your fleet, and use the link it shows.</p>"), "")
    rd_json = _js_string(rd)
    return _page("Autonomy", (
        "<h1>Autonomy</h1><p>Use your passkey to open this dashboard.</p>"
        "<button id=\"go\">Continue with passkey</button>"), f"""
var rd={rd_json};
async function login(){{
  document.getElementById('go').disabled=true;
  try{{
    var o=(await post('/oauth2/login/options',{{}})).options;
    o.challenge=unb64u(o.challenge);
    (o.allowCredentials||[]).forEach(function(c){{c.id=unb64u(c.id);}});
    var cred=await navigator.credentials.get({{publicKey:o}});
    var r=cred.response;
    var out=await post('/oauth2/login/verify',{{rd:rd,credential:{{id:cred.id,rawId:b64u(cred.rawId),type:cred.type,
      authenticatorAttachment:cred.authenticatorAttachment||null,clientExtensionResults:cred.getClientExtensionResults(),
      response:{{clientDataJSON:b64u(r.clientDataJSON),authenticatorData:b64u(r.authenticatorData),signature:b64u(r.signature),
      userHandle:r.userHandle?b64u(r.userHandle):null}}}}}});
    location.replace(out.redirect||'/');
  }}catch(e){{fail(e.message||String(e));}}
}}
document.getElementById('go').addEventListener('click',login);
""")


def _enroll_page(token: str) -> str:
    token_json = _js_string(token)
    return _page("Autonomy", (
        "<h1>Enroll your passkey</h1><p>This passkey will be the only way in at this address. "
        "Enroll it on the device you will use to reach the dashboard.</p>"
        "<button id=\"go\">Create passkey</button>"), f"""
var token={token_json};
async function enroll(){{
  document.getElementById('go').disabled=true;
  try{{
    var o=(await post('/oauth2/enroll/options',{{token:token}})).options;
    o.challenge=unb64u(o.challenge);o.user.id=unb64u(o.user.id);
    (o.excludeCredentials||[]).forEach(function(c){{c.id=unb64u(c.id);}});
    var cred=await navigator.credentials.create({{publicKey:o}});
    var r=cred.response;
    var out=await post('/oauth2/enroll/verify',{{token:token,credential:{{id:cred.id,rawId:b64u(cred.rawId),type:cred.type,
      authenticatorAttachment:cred.authenticatorAttachment||null,clientExtensionResults:cred.getClientExtensionResults(),
      response:{{clientDataJSON:b64u(r.clientDataJSON),attestationObject:b64u(r.attestationObject),
      transports:(r.getTransports&&r.getTransports())||[]}}}}}});
    history.replaceState(null,'','/oauth2/start');
    location.replace(out.redirect||'/');
  }}catch(e){{fail(e.message||String(e));}}
}}
document.getElementById('go').addEventListener('click',enroll);
""")


def _closed_page() -> str:
    return _page("Autonomy", (
        "<h1>Enrollment is closed</h1><p>This link is not valid: enrollment was never opened, "
        "has already been used, or has expired. Open it again from the dashboard on the machine "
        "itself.</p>"), "")


# ── entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", required=True, help="the gate runtime directory")
    parser.add_argument("--port", type=int, required=True, help="loopback listener port")
    args = parser.parse_args(argv)
    import sys

    import uvicorn

    runtime = GateRuntime(args.runtime)
    record = runtime.record()
    # One line at start, so the container's log proves what it serves for
    # (Windows run 9: an empty log left the helper's state unknowable).
    print(f"passkey gate: listening on 127.0.0.1:{args.port}; runtime {runtime.directory}; "
          f"rp_id={record.get('rp_id')!r} credentials={len(record.get('credentials') or [])} "
          f"enrollment={'open' if (record.get('enrollment') or {}).get('open') else 'closed'} "
          f"dashboard_upstream={record.get('dashboard_upstream')!r}", file=sys.stderr, flush=True)
    uvicorn.run(build_app(args.runtime), host="127.0.0.1", port=args.port,
                log_level="warning", lifespan="off", access_log=False, proxy_headers=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
