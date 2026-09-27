"""Guards for the one-click software update (follow-origin fast-forward).

The point of these tests is the safety property: ``perform_update`` must NEVER
``reset --hard`` away work that origin does not have. The guard is git history
itself — an author machine is always ahead of origin — so the tests build real
tiny git repos and assert the follower fast-forwards while the author and a
dirty tree are refused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.dashboard import software_update as su


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def _identity(cwd: Path) -> None:
    # Local, hermetic identity — and never sign: a global commit.gpgsign=true
    # would otherwise fail these throwaway commits with "gpg failed to sign".
    _git(cwd, "config", "user.email", "t@t")
    _git(cwd, "config", "user.name", "t")
    _git(cwd, "config", "commit.gpgsign", "false")


def _commit(cwd: Path, name: str, text: str) -> None:
    (cwd / name).write_text(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch):
    """A bare 'origin' with 3 commits, plus a checkout the tests reposition."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "master", str(seed))
    _identity(seed)
    for i in range(3):
        _commit(seed, f"f{i}.txt", str(i))
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _identity(checkout)
    # Point the module at this checkout and disable the network fetch (the bare
    # origin is local; a fetch of a local remote is fine, but keep it explicit).
    monkeypatch.setattr(su, "_REPO_ROOT", checkout)
    # The applied-update record lives under DATA_ROOT; keep it in the test tree.
    monkeypatch.setattr(su, "_last_update_path", lambda: tmp_path / "last-update.json")
    return {"origin": origin, "checkout": checkout, "tmp": tmp_path}


def test_follower_fast_forwards(fleet):
    checkout = fleet["checkout"]
    _git(checkout, "reset", "--hard", "-q", "HEAD~2")   # 2 behind, clean

    status = su.update_status(fetch=True)
    assert status["mode"] == "follower"
    assert status["behind"] == 2 and status["ahead"] == 0
    assert status["can_update"] is True

    result = su.perform_update()
    assert result["updated"] is True
    assert result["count"] == 2
    assert su.update_status(fetch=False)["behind"] == 0


def test_author_is_refused(fleet):
    checkout = fleet["checkout"]
    _commit(checkout, "local.txt", "unshipped")          # 1 ahead, clean

    status = su.update_status(fetch=True)
    assert status["mode"] == "author"
    assert status["ahead"] == 1
    assert status["can_update"] is False

    with pytest.raises(su.SoftwareUpdateError):
        su.perform_update()


def test_dirty_tree_is_refused(fleet):
    checkout = fleet["checkout"]
    _git(checkout, "reset", "--hard", "-q", "HEAD~1")     # behind, but…
    (checkout / "f0.txt").write_text("edited but uncommitted")

    status = su.update_status(fetch=True)
    assert status["dirty"] is True
    assert status["can_update"] is False
    with pytest.raises(su.SoftwareUpdateError):
        su.perform_update()


def test_up_to_date_is_a_noop(fleet):
    status = su.update_status(fetch=True)
    assert status["behind"] == 0
    result = su.perform_update()
    assert result["updated"] is False



# ── The node keeps its public origin; the preference and the poller ────────
# (auto-2uj1b, graph://89d3c8df-544 §6 and driver S7; operator decision
# 2026-09-27: "check and notify" plus "install when available").

import asyncio  # noqa: E402

from tools.dashboard import software_update_settings as prefs  # noqa: E402
from tools.graph.schemas import SchemaValidationError  # noqa: E402


def test_a_checkout_without_origin_gets_the_public_remote(fleet, monkeypatch):
    checkout = fleet["checkout"]
    _git(checkout, "remote", "remove", "origin")
    # The "public" URL is the local bare origin, so the fetch stays hermetic.
    monkeypatch.setattr(su, "_PUBLIC_ORIGIN_URL", str(fleet["origin"]))
    _git(checkout, "reset", "--hard", "-q", "HEAD~1")

    status = su.update_status(fetch=True)

    assert _git(checkout, "remote", "get-url", "origin").strip() == str(fleet["origin"])
    assert status["fetched"] is True and status["behind"] == 1
    assert status["checked_at"]  # FETCH_HEAD now exists


def test_an_applied_update_is_recorded_with_how_it_happened(fleet):
    _git(fleet["checkout"], "reset", "--hard", "-q", "HEAD~2")
    result = su.perform_update(automatic=True)
    record = su.last_update()
    assert record["automatic"] is True
    assert (record["from"], record["to"], record["count"]) == (result["from"], result["to"], 2)
    assert su.update_status(fetch=False)["last_update"] == record


def test_preference_defaults_and_bounds():
    assert prefs.resolve(None) == {"auto_check": True, "auto_install": False, "interval_minutes": 360}
    # Install without check is not an operator choice.
    assert prefs.resolve({"auto_check": False, "auto_install": True})["auto_install"] is False
    prefs.SoftwareUpdatePreferenceV1.validate({"auto_check": True, "auto_install": True, "interval_minutes": 30})
    with pytest.raises(SchemaValidationError):
        prefs.SoftwareUpdatePreferenceV1.validate({"interval_minutes": 29})
    with pytest.raises(SchemaValidationError):
        prefs.SoftwareUpdatePreferenceV1.validate({"auto_check": "yes"})


class _Sim:
    """A simulated day for the poller: a clock, and a sleep that advances it."""

    def __init__(self, seconds: float):
        self.now = 0.0
        self.end = seconds

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.now += s
        if self.now >= self.end:
            raise asyncio.CancelledError


def _run(pref, statuses, *, update=None, seconds=24 * 3600):
    sim = _Sim(seconds)
    fetches, published, installs = [], [], []

    def status_fn(*, fetch):
        if fetch:
            fetches.append(sim.now)
        return statuses(len(fetches), fetch)

    def update_fn(*, automatic):
        installs.append(automatic)
        return update or {"updated": True, "from": "a", "to": "b", "count": 1}

    async def publish(payload):
        published.append(payload)

    try:
        asyncio.run(su.run_poller(
            publish, read_preference=lambda: pref, status_fn=status_fn,
            update_fn=update_fn, sleep=sim.sleep, clock=sim.clock,
        ))
    except asyncio.CancelledError:
        pass
    return fetches, published, installs


def test_with_checks_off_nothing_is_fetched_all_day():
    fetches, published, installs = _run(
        {"auto_check": False, "auto_install": False, "interval_minutes": 360},
        lambda n, fetch: {"behind": 0, "can_update": False},
    )
    assert fetches == [] and published == [] and installs == []


def test_with_checks_on_one_fetch_per_interval():
    fetches, _, _ = _run(
        {"auto_check": True, "auto_install": False, "interval_minutes": 360},
        lambda n, fetch: {"behind": 0, "can_update": False},
    )
    assert len(fetches) == 4  # 24 h / 6 h
    assert all(b - a >= 360 * 60 for a, b in zip(fetches, fetches[1:]))


def test_the_event_fires_only_when_behind_changes():
    behind = {1: 0, 2: 0, 3: 2, 4: 2}
    _, published, installs = _run(
        {"auto_check": True, "auto_install": False, "interval_minutes": 360},
        lambda n, fetch: {"behind": behind[n], "can_update": behind[n] > 0},
    )
    assert [p["behind"] for p in published] == [0, 2]
    assert installs == []


def test_auto_install_applies_an_available_update_and_announces_it():
    state = {"installed": False}

    def statuses(n, fetch):
        if state["installed"] or not fetch and n >= 1:
            return {"behind": 0, "can_update": False}
        return {"behind": 3, "can_update": True}

    def update(**kw):
        state["installed"] = True
        return {"updated": True, "from": "a", "to": "b", "count": 3}

    sim_pref = {"auto_check": True, "auto_install": True, "interval_minutes": 360}
    fetches, published, installs = [], [], []
    sim = _Sim(3600)

    def status_fn(*, fetch):
        if fetch:
            fetches.append(sim.now)
        return statuses(len(fetches), fetch)

    def update_fn(*, automatic):
        installs.append(automatic)
        return update()

    async def publish(payload):
        published.append(payload)

    try:
        asyncio.run(su.run_poller(publish, read_preference=lambda: sim_pref, status_fn=status_fn,
                                  update_fn=update_fn, sleep=sim.sleep, clock=sim.clock))
    except asyncio.CancelledError:
        pass
    assert installs == [True]
    assert published and published[0]["installed"]["count"] == 3
    assert published[0]["behind"] == 0


# ── Routes: the preference and the preference-carrying update status ────────

class _Req:
    def __init__(self, method="GET", body=None, query=None):
        self.method = method
        self._body = body
        self.query_params = query or {}

    async def json(self):
        return self._body


def _server(monkeypatch, *, operator=True):
    from tools.dashboard import server
    from tools.dashboard.server import api_auth

    monkeypatch.setattr(api_auth, "require_authenticated_api_caller", lambda r: None)
    monkeypatch.setattr(
        api_auth, "require_global_api_authority",
        (lambda r: None) if operator else (lambda r: server.JSONResponse({"error": "no"}, status_code=403)),
    )
    return server


def test_update_status_carries_the_preference(monkeypatch):
    import json as _json

    server = _server(monkeypatch)
    monkeypatch.setattr(su, "update_status", lambda *, fetch: {"behind": 0, "can_update": False, "fetched": fetch})
    monkeypatch.setattr(prefs, "read_preference",
                        lambda: {"auto_check": False, "auto_install": False, "interval_minutes": 360})
    resp = asyncio.run(server.api_software_update_status(_Req(query={"fetch": "0"})))
    body = _json.loads(resp.body)
    assert body["fetched"] is False  # ?fetch=0 never reaches GitHub
    assert body["auto_check"] is False and body["auto_install"] is False


def test_preference_put_is_the_operators_and_validates(monkeypatch):
    import json as _json

    written = []
    monkeypatch.setattr(prefs, "write_preference", lambda body: written.append(body) or prefs.resolve(body))
    server = _server(monkeypatch, operator=False)
    assert asyncio.run(server.api_software_preference(_Req("PUT", {"auto_install": True}))).status_code == 403
    assert written == []

    server = _server(monkeypatch, operator=True)
    resp = asyncio.run(server.api_software_preference(_Req("PUT", {"auto_check": True, "auto_install": True})))
    assert resp.status_code == 200 and _json.loads(resp.body)["auto_install"] is True

    def refuse(body):
        raise SchemaValidationError("'interval_minutes' must be an integer >= 30")

    monkeypatch.setattr(prefs, "write_preference", refuse)
    assert asyncio.run(server.api_software_preference(_Req("PUT", {"interval_minutes": 5}))).status_code == 400
