"""Live-path proof for the shared Node sign-on command.

Sign-on is a PERSONAL act (design §2, §1c): one unlock of the personal root
armor yields one persona per organization. These tests drive the full path
headlessly (§21) with a throwaway personal identity belonging to several test
organizations — no browser, no second passphrase, no organization root key.
"""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.graph.schemas.network_identity import ORG_ROOT_ARMOR_PURPOSE
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
from tools.network.idkit.sealing import derive_encapsulation_keypair, seal

REPO = Path(__file__).resolve().parents[6]
COMMAND = (
    REPO
    / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "node" / "signon.mjs"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port: int):
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, f"server on port {port} did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), f"server on port {port} did not stop"


class OrgFixture:
    """One throwaway organization the personal identity belongs to.

    ``genesis_id`` present means the ledger is founded, so the identity has a
    persona there. ``rekey`` is the organization's opportunistic-refresh
    policy row (§1d trigger 2) — ``None`` for an organization that publishes
    no interval.
    """

    def __init__(self, slug: str, *, genesis_id: str | None,
                 rekey: dict | None = None, bound: bool = True,
                 serve_required: bool = False, serve_status: int = 200,
                 legacy_key: bool = False,
                 org_passphrase: str | None = None):
        self.slug = slug
        self.genesis_id = genesis_id
        self.rekey = rekey
        self.org_uuid = str(uuid.uuid4())
        self.root = KeyPair.generate()
        self.bound = bound
        # Whether this organization's serving certificate is due for renewal,
        # and whether the status route answers at all — a registry that is
        # down must not cost the operator their sign-on.
        self.serve_required = serve_required
        self.serve_status = serve_status
        # A revision-1 organization: its root is armored under its OWN
        # passphrase, which a personal unlock has only if they happen to match.
        self.legacy_key = legacy_key
        self.org_passphrase = org_passphrase

    def sealed_org_key(self, personal_root: KeyPair) -> dict:
        """This org's root, sealed to the personal root exactly as founding
        seals it — openable by the seed sign-on already holds, with no
        organization passphrase anywhere."""
        _private, public = derive_encapsulation_keypair(
            bytes.fromhex(personal_root.private_hex), ORG_ROOT_ARMOR_PURPOSE,
        )
        return {
            "root_pub": self.root.public_hex,
            "sealed_root_key": seal(
                bytes.fromhex(self.root.private_hex),
                public,
                ORG_ROOT_ARMOR_PURPOSE,
            ).hex(),
            "owner_kem_pub": public,
            "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        }

    def persona_pub(self, personal_root: KeyPair) -> str:
        return derive_persona(
            bytes.fromhex(personal_root.private_hex), self.genesis_id,
        ).public_hex


def _identity_source(*, personal_armor: str, personal_root_pub: str,
                     orgs: list[OrgFixture], requests: list[str],
                     rekeyed: list[dict], served: list[dict] | None = None,
                     migrations: list[dict] | None = None,
                     personal_root: KeyPair | None = None,
                     registry_url: str = "http://127.0.0.1:9") -> Starlette:
    """The dashboard-shaped identity source the Node command reads.

    Every route records its path in *requests*, so a test can assert what the
    ceremony did and — for ``/api/network/org-key`` — what it never touched.
    """
    by_slug = {org.slug: org for org in orgs}

    def _org(request: Request) -> OrgFixture | None:
        return by_slug.get(request.query_params.get("org") or "")

    async def org_list(request: Request):
        requests.append(str(request.url.path))
        return JSONResponse({
            "orgs": [{"org": {"slug": org.slug}} for org in orgs],
        })

    async def personal(request: Request):
        requests.append(str(request.url.path))
        return JSONResponse({
            "armored_private_key": personal_armor,
            "root_pub": personal_root_pub,
        })

    async def org_key(request: Request):
        # Sign-on reaches this route ONLY for an organization whose serving
        # certificate is actually due for renewal. For every other org it
        # answers so that a regression shows up as an assertion on the trace
        # rather than as an unrelated transport error.
        requests.append(str(request.url.path))
        org = _org(request)
        if org is None:
            return JSONResponse({"error": "unknown org"}, status_code=404)
        if org.legacy_key:
            return JSONResponse({
                "label": "default",
                "armored_private_key": mint_password_armor(
                    org.root, org.org_passphrase, iterations=10_000),
                "root_pub": org.root.public_hex,
            })
        if personal_root is not None:
            # The real shape: sealed to the personal root, no org passphrase.
            return JSONResponse(
                {"label": "default", **org.sealed_org_key(personal_root)},
            )
        return JSONResponse({
            "label": "default",
            "armored_private_key": mint_password_armor(
                org.root, "the org armor nobody should open", iterations=10_000,
            ),
            "root_pub": org.root.public_hex,
        })

    async def ledger_heads(request: Request):
        requests.append(str(request.url.path))
        org = _org(request)
        if org is None or org.genesis_id is None:
            return JSONResponse(
                {"ok": False, "error": "organization ledger is not founded"},
                status_code=404,
            )
        return JSONResponse({
            "genesis_id": org.genesis_id, "heads": [org.genesis_id],
        })

    async def binding(request: Request):
        requests.append(str(request.url.path))
        org = _org(request)
        if org is None or not org.bound:
            return JSONResponse({"error": "unknown org"}, status_code=404)
        return JSONResponse({
            "org_uuid": org.org_uuid,
            "root_pub": org.root.public_hex,
            "registry_url": registry_url,
        })

    async def rekey_policy(request: Request):
        requests.append(str(request.url.path))
        org = _org(request)
        if org is None or org.rekey is None:
            return JSONResponse({"error": "no policy"}, status_code=404)
        return JSONResponse(org.rekey)

    async def rekey(request: Request):
        requests.append(str(request.url.path))
        rekeyed.append(await request.json())
        return JSONResponse({"ok": True})

    async def serve_cert_state(request: Request):
        requests.append(str(request.url.path))
        org = _org(request)
        if org is None or not org.bound:
            return JSONResponse({"required": False, "status": "unregistered"})
        if org.serve_status != 200:
            return JSONResponse(
                {"error": "registry unavailable"}, status_code=org.serve_status,
            )
        return JSONResponse({
            "required": org.serve_required,
            "status": "expiring" if org.serve_required else "ready",
        })

    async def serve_cert_post(request: Request):
        requests.append("POST " + str(request.url.path))
        body = await request.json()
        if served is not None:
            served.append(body)
        return JSONResponse({"ok": True})

    async def org_key_migrate(request: Request):
        requests.append("POST " + str(request.url.path))
        body = await request.json()
        if migrations is not None:
            migrations.append(body)
        return JSONResponse({"ok": True, "root_pub": body.get("root_pub")})

    return Starlette(routes=[
        Route("/api/orgs", org_list),
        Route("/api/network/org-key/migrate", org_key_migrate, methods=["POST"]),
        Route("/api/network/serve-cert", serve_cert_state),
        Route("/api/network/serve-cert", serve_cert_post, methods=["POST"]),
        Route("/api/identity/personal", personal),
        Route("/api/network/org-key", org_key),
        Route("/api/network/ledger/heads", ledger_heads),
        Route("/api/network/binding", binding),
        Route("/api/network/rekey-policy", rekey_policy),
        Route("/api/network/rekey", rekey, methods=["POST"]),
    ])


def _clean_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in ("AUTONOMY_ORG_PASSPHRASE", "AUTONOMY_PERSONAL_PASSPHRASE")
    }


def _run(identity_url: str, passphrase: str, *extra: str) -> subprocess.CompletedProcess:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, f"{passphrase}\n".encode())
    finally:
        os.close(write_fd)
    try:
        return subprocess.run(
            [
                "node", str(COMMAND),
                "--server", identity_url,
                "--ttl", "3600",
                "--passphrase-fd", str(read_fd),
                *extra,
            ],
            cwd=REPO,
            env=_clean_environment(),
            pass_fds=(read_fd,),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.close(read_fd)


def _sign_on(orgs: list[OrgFixture], *, passphrase: str,
             personal_root: KeyPair, extra: tuple[str, ...] = (),
             served: list[dict] | None = None, sealed_org_keys: bool = False,
             migrations: list[dict] | None = None,
             ) -> tuple[dict, list[str], list[dict]]:
    """Run one headless sign-on; return (output, request trace, re-keys)."""
    requests: list[str] = []
    rekeyed: list[dict] = []
    identity = _identity_source(
        personal_armor=mint_password_armor(
            personal_root, passphrase, iterations=10_000,
        ),
        personal_root_pub=personal_root.public_hex,
        orgs=orgs,
        requests=requests,
        rekeyed=rekeyed,
        served=served,
        migrations=migrations,
        personal_root=personal_root if sealed_org_keys else None,
    )
    with _live_server(identity, _free_port()) as identity_url:
        command = _run(identity_url, passphrase, *extra)
    assert command.returncode == 0, command.stdout + "\n" + command.stderr
    return json.loads(command.stdout), requests, rekeyed


def _three_founded_orgs() -> list[OrgFixture]:
    return [
        OrgFixture("alpha-org", genesis_id="a1" * 32),
        OrgFixture("beta-org", genesis_id="b2" * 32),
        OrgFixture("gamma-org", genesis_id="c3" * 32),
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_one_unlock_derives_one_persona_per_organization():
    """A throwaway personal identity belonging to three organizations gets
    three DISTINCT personas from a single passphrase entry, each of them
    exactly HKDF(personal_root_seed, genesis_id) — byte-identical to idkit's
    derivation."""
    personal_root = KeyPair.generate()
    orgs = _three_founded_orgs()
    output, requests, _ = _sign_on(
        orgs, passphrase="one personal password", personal_root=personal_root,
    )

    signed_on = output["signOn"]
    assert signed_on["personalRootPub"] == personal_root.public_hex
    assert signed_on["diagnostics"]["personaCount"] == 3
    assert [org["orgSlug"] for org in signed_on["orgs"]] == [
        "alpha-org", "beta-org", "gamma-org",
    ]
    personas = {
        org["orgSlug"]: org["personaPub"] for org in signed_on["orgs"]
    }
    assert personas == {
        org.slug: org.persona_pub(personal_root) for org in orgs
    }
    assert len(set(personas.values())) == 3, "personas must differ per org"
    # One unlock: the personal armor is fetched exactly once, so there is no
    # second place a passphrase could have been entered.
    assert requests.count("/api/identity/personal") == 1
    # One session key, worn as three personas.
    assert len({org["subject"]["id"] for org in signed_on["orgs"]}) == 3
    assert signed_on["sessionPub"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_sign_on_never_decrypts_an_organization_root_key():
    """The removed ``_openOrgRoot(orgKey, passphrase)`` path is not taken:
    sign-on never even fetches an organization's key, however many
    organizations it covers."""
    personal_root = KeyPair.generate()
    output, requests, _ = _sign_on(
        _three_founded_orgs(),
        passphrase="one personal password",
        personal_root=personal_root,
    )
    assert "/api/network/org-key" not in requests
    assert output["signOn"]["diagnostics"]["orgRootsOpened"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_actor_is_the_per_organization_persona():
    """The delegation cert's subject is that organization's persona public
    key — not a random browser label and not the personal key — and the
    persona is what SIGNED it, so the chain resolves to the persona (§7)."""
    from tools.network.idkit import DelegationCert, verify_chain

    personal_root = KeyPair.generate()
    orgs = _three_founded_orgs()
    output, _, _ = _sign_on(
        orgs, passphrase="one personal password", personal_root=personal_root,
    )
    by_slug = {org.slug: org for org in orgs}
    for entry in output["signOn"]["orgs"]:
        persona_pub = by_slug[entry["orgSlug"]].persona_pub(personal_root)
        assert entry["subject"] == {"kind": "operator", "id": persona_pub}
        assert not entry["subject"]["id"].startswith("browser-")
        assert entry["subject"]["id"] != personal_root.public_hex
        cert = DelegationCert.from_json(entry["certWire"])
        # Anchored at the persona: verification against the persona public
        # key is what "the actor is the persona" means cryptographically.
        result = verify_chain(cert, persona_pub, org=entry["org"])
        assert result.subject_id == persona_pub
        assert result.leaf_pub == output["signOn"]["sessionPub"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_opportunistic_rekey_is_evaluated_per_organization():
    """One unlock re-keys EXACTLY the organizations whose interval has
    elapsed (§1d trigger 2), and leaves the others alone."""
    now = int(time.time())
    personal_root = KeyPair.generate()
    orgs = [
        OrgFixture("elapsed-org", genesis_id="a1" * 32, rekey={
            "interval_seconds": 3600, "last_rekey_at": now - 7200,
        }),
        OrgFixture("fresh-org", genesis_id="b2" * 32, rekey={
            "interval_seconds": 3600, "last_rekey_at": now - 60,
        }),
        OrgFixture("no-policy-org", genesis_id="c3" * 32, rekey=None),
    ]
    output, _, rekeyed = _sign_on(
        orgs,
        passphrase="one personal password",
        personal_root=personal_root,
        extra=("--rekey-endpoint", "/api/network/rekey"),
    )

    assert [row["org"] for row in rekeyed] == ["elapsed-org"]
    assert rekeyed[0]["genesis_id"] == "a1" * 32
    assert rekeyed[0]["persona_pub"] == orgs[0].persona_pub(personal_root)
    assert rekeyed[0]["reason"] == "OPPORTUNISTIC"

    decisions = {
        entry["orgSlug"]: entry["rekey"] for entry in output["signOn"]["orgs"]
    }
    assert decisions["elapsed-org"]["fired"] is True
    assert decisions["elapsed-org"]["due"] is True
    assert decisions["elapsed-org"]["elapsedSeconds"] >= 7200
    assert decisions["fresh-org"]["fired"] is False
    assert decisions["fresh-org"]["due"] is False
    assert decisions["fresh-org"]["reason"] == "interval-not-elapsed"
    assert decisions["no-policy-org"]["evaluated"] is False
    assert decisions["no-policy-org"]["reason"] == "no-interval-configured"
    assert output["signOn"]["diagnostics"]["rekeyedOrgs"] == ["elapsed-org"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_rekey_evaluation_is_reported_without_an_executor():
    """With no re-key adapter installed the interval is still evaluated per
    organization — the decision is reported and nothing fires."""
    now = int(time.time())
    output, _, rekeyed = _sign_on(
        [OrgFixture("elapsed-org", genesis_id="a1" * 32, rekey={
            "interval_seconds": 3600, "last_rekey_at": now - 7200,
        })],
        passphrase="one personal password",
        personal_root=KeyPair.generate(),
    )
    decision = output["signOn"]["orgs"][0]["rekey"]
    assert decision["due"] is True
    assert decision["fired"] is False
    assert decision["reason"] == "no-rekey-adapter"
    assert rekeyed == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_unfounded_organization_is_skipped_not_labelled():
    """No genesis ⇒ no persona (§2). An unfounded organization is reported as
    skipped; it never signs on behind a stand-in browser label."""
    personal_root = KeyPair.generate()
    output, _, _ = _sign_on(
        [
            OrgFixture("founded-org", genesis_id="a1" * 32),
            OrgFixture("unfounded-org", genesis_id=None),
        ],
        passphrase="one personal password",
        personal_root=personal_root,
    )
    signed_on = output["signOn"]
    assert [org["orgSlug"] for org in signed_on["orgs"]] == ["founded-org"]
    assert signed_on["skipped"] == [
        {"orgSlug": "unfounded-org", "reason": "ledger-not-founded"},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_named_organizations_restrict_one_unlock():
    """``--org`` restricts which organizations the one unlock covers; the
    personal armor is still opened exactly once."""
    personal_root = KeyPair.generate()
    output, requests, _ = _sign_on(
        _three_founded_orgs(),
        passphrase="one personal password",
        personal_root=personal_root,
        extra=("--org", "beta-org", "--org", "gamma-org"),
    )
    assert [org["orgSlug"] for org in output["signOn"]["orgs"]] == [
        "beta-org", "gamma-org",
    ]
    assert requests.count("/api/identity/personal") == 1
    assert "/api/orgs" not in requests


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_wrong_passphrase_fails_closed():
    """A passphrase that does not open the personal armor signs on nowhere —
    no persona is derived and no organization is touched."""
    personal_root = KeyPair.generate()
    requests: list[str] = []
    rekeyed: list[dict] = []
    identity = _identity_source(
        personal_armor=mint_password_armor(
            personal_root, "the real personal password", iterations=10_000,
        ),
        personal_root_pub=personal_root.public_hex,
        orgs=_three_founded_orgs(),
        requests=requests,
        rekeyed=rekeyed,
    )
    with _live_server(identity, _free_port()) as identity_url:
        command = _run(identity_url, "opens nothing")
    assert command.returncode != 0
    combined = command.stdout + command.stderr
    assert "passphrase" in combined or "password" in combined
    assert "/api/network/ledger/heads" not in requests
    assert rekeyed == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_missing_passphrase_touches_nothing():
    personal_root = KeyPair.generate()
    requests: list[str] = []
    identity = _identity_source(
        personal_armor=mint_password_armor(
            personal_root, "unused", iterations=10_000,
        ),
        personal_root_pub=personal_root.public_hex,
        orgs=_three_founded_orgs(),
        requests=requests,
        rekeyed=[],
    )
    with _live_server(identity, _free_port()) as identity_url:
        command = subprocess.run(
            ["node", str(COMMAND), "--server", identity_url],
            cwd=REPO,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=20,
        )
    assert command.returncode != 0
    assert "missing passphrase source" in command.stderr
    assert requests == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_due_serving_certificate_is_renewed_by_the_personal_unlock():
    """Serving certificates expire on their own 30-day clock, and used to do
    it unattended because renewal hung off an ORGANIZATION sign-on that no
    longer happens.

    One personal unlock now renews them. The org root a renewal needs is not
    derived from the persona -- both are siblings off the personal root -- but
    it is SEALED to the personal root, so the seed this unlock already holds
    opens it with no second passphrase. Only the organization actually due is
    touched.
    """
    personal_root = KeyPair.generate()
    orgs = _three_founded_orgs()
    orgs[1].serve_required = True          # beta-org alone is expiring
    served: list[dict] = []
    output, requests, _ = _sign_on(
        orgs, passphrase="one personal password", personal_root=personal_root,
        served=served, sealed_org_keys=True,
    )

    signed_on = output["signOn"]
    diagnostics = signed_on["diagnostics"]
    assert diagnostics["serveCertsRenewed"] == ["beta-org"]
    assert diagnostics["serveCertsFailed"] == []

    # Exactly one credential was minted, and NO org root was opened to mint
    # it (auto-55vwi): the persona the sign-on already derived signed it.
    assert len(served) == 1
    assert diagnostics["orgRootsOpened"] == 0
    assert requests.count("/api/network/org-key") == 0

    # The credential names THAT organization's persona — which also SIGNED
    # it; there is no viewer certificate.
    assert "viewer_cert" not in served[0]
    cert = json.loads(served[0]["cert"])
    assert cert["subject"] == {
        "kind": "persona", "id": orgs[1].persona_pub(personal_root),
    }
    assert cert["org"] == orgs[1].org_uuid
    assert cert["scope"] == ["tunnel:serve"]
    assert cert["not_after"] > cert["not_before"]

    # Sign-on itself is undisturbed: still three personas, still one unlock.
    assert diagnostics["personaCount"] == 3
    assert requests.count("/api/identity/personal") == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_failed_renewal_is_reported_and_does_not_break_sign_on():
    """A registry that is down cannot cost the operator their session -- and
    must not fail the way this failed before, into a console warning nobody
    read for three weeks. The failure is REPORTED, per organization."""
    personal_root = KeyPair.generate()
    orgs = _three_founded_orgs()
    orgs[2].serve_status = 503             # gamma-org's status route is down
    output, _requests, _ = _sign_on(
        orgs, passphrase="one personal password", personal_root=personal_root,
        sealed_org_keys=True,
    )

    signed_on = output["signOn"]
    diagnostics = signed_on["diagnostics"]
    assert diagnostics["personaCount"] == 3, "sign-on survives a dead registry"
    assert signed_on["sessionPub"]

    failed = diagnostics["serveCertsFailed"]
    assert [entry["org"] for entry in failed] == ["gamma-org"]
    assert failed[0]["status"] == "failed"
    assert failed[0]["error"], "a failure has to say what went wrong"






@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_session_certificate_carries_every_session_scope():
    """The scopes a session certificate carries are fixed at sign-on and can
    never be widened afterwards: idkit requires a child certificate's scope to
    be a strict subset of its parent's. A capability whose scope is missing
    here is unreachable for the whole session, and the failure surfaces at the
    issuer as a scope refusal rather than as anything naming sign-on.

    turn:allocate is EXECUTION -- an authenticated session allocating relay
    capacity for itself, granting authority to nobody, the same shape as
    tunnel:serve.
    """
    personal_root = KeyPair.generate()
    output, _requests, _ = _sign_on(
        _three_founded_orgs(), passphrase="one personal password",
        personal_root=personal_root,
    )

    for org in output["signOn"]["orgs"]:
        cert = json.loads(org["certWire"])
        assert cert["scope"] == [
            "delegate:agent",
            "link:publish",
            "link:revoke",
            "tunnel:serve",
            "turn:allocate",
            "viewer:identify",
        ], org["orgSlug"]
        # idkit parses strictly: an unsorted or duplicated scope list is
        # malformed, so the ordering above is a contract, not a preference.
        assert cert["scope"] == sorted(set(cert["scope"]))
