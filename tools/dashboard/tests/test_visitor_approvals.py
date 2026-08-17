"""Admitting a person is asked for by an agent and decided by the operator.

The route that mints a way in is closed to every session, which is right --
handing somebody a credential is the operator's to allow. It also left that
act reachable only by typing a command with a photo encoded into it, which is
not a thing anybody does from a phone. This carries the same request on the
approval rendezvous instead.

What these hold to: an agent cannot admit anybody by itself, the operator sees
who and why before deciding, a refusal happens before it reaches them rather
than after, and the token comes back exactly once to whoever asked.
"""

from __future__ import annotations

import pytest

from tools.dashboard import visitor_approvals as va


# ── what the operator is asked ────────────────────────────────────

def test_the_operator_is_shown_who_is_being_admitted_and_why():
    stored, staged = va.prepare_create(
        "auto-0807-165113",
        {"display_name": "Shari Vietry", "reason": "reviewing the OSS work"},
    )
    assert staged["display_name"] == "Shari Vietry"
    assert staged["reason"] == "reviewing the OSS work"
    assert staged["asked_by"] == "auto-0807-165113"
    assert stored["display_name"] == "Shari Vietry"


def test_the_photo_is_not_put_on_the_decision_surface():
    """A decision surface carries what is being decided -- who, and why. The
    photo is several hundred kilobytes and is not that."""
    avatar = "data:image/jpeg;base64," + "A" * 4000
    stored, staged = va.prepare_create("auto-x", {
        "display_name": "Shari Vietry", "avatar": avatar,
    })
    assert staged["has_photo"] is True
    assert "avatar" not in staged
    assert stored["avatar"] == avatar          # kept, for the minting step


# ── refusing early ────────────────────────────────────────────────

@pytest.mark.parametrize(("request_body", "expected"), [
    ({}, "display_name is required"),
    ({"display_name": "   "}, "display_name is required"),
    ({"display_name": "x" * 200}, "at most"),
    ({"display_name": "A", "avatar": "https://example.com/face.jpg"}, "data:image/"),
    ({"display_name": "A", "avatar": 42}, "data:image/"),
    ({"display_name": "A", "sneaky": 1}, "got also: sneaky"),
])
def test_a_request_that_could_never_succeed_never_reaches_the_operator(
    request_body, expected,
):
    """Refused at the asking, not at the deciding. Anything else spends the
    operator's attention on a request that was always going to fail."""
    with pytest.raises(ValueError) as exc:
        va.prepare_create("auto-x", request_body)
    assert expected in str(exc.value)


# ── the decision is the authority ─────────────────────────────────

class _Req:
    def __init__(self, session):
        self._session = session


def test_only_a_human_operator_session_may_admit_somebody(monkeypatch):
    """Without this the approval is a formality: anything able to reach the
    decision route could let a stranger in."""
    import tools.dashboard.unlock_routes as unlock

    monkeypatch.setattr(unlock, "gate_disabled", lambda: False)

    monkeypatch.setattr(unlock, "session_from_request", lambda r: None)
    assert va.authorize_decision(_Req(None), {}, {}) is not None

    monkeypatch.setattr(unlock, "session_from_request",
                        lambda r: {"method": "token"})
    assert va.authorize_decision(_Req(None), {}, {}) is not None

    for method in ("bootstrap", "passkey", "password"):
        monkeypatch.setattr(unlock, "session_from_request",
                            lambda r, m=method: {"method": m})
        assert va.authorize_decision(_Req(None), {}, {}) is None


@pytest.mark.asyncio
async def test_a_declined_request_admits_nobody():
    out = await va.execute({"request": {"display_name": "Shari Vietry"}},
                           {"approved": False})
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_approval_mints_the_way_in_and_returns_it_once(tmp_path, monkeypatch):
    """The token is a secret shown once, so it travels in the result of the
    request that asked for it and is stored nowhere else."""
    from tools.dashboard.dao import mission_control_db as db

    db_path = tmp_path / "mc.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_db_path", lambda p=None: db_path)

    out = await va.execute(
        {"request": {"display_name": "Shari Vietry", "reason": "reviewing"}},
        {"approved": True},
    )
    assert out["ok"] is True
    assert out["display_name"] == "Shari Vietry"
    assert out["participant_id"].startswith("guest:")
    assert len(out["token"]) == 64
    # And the person is really there, findable by the id that is safe to show.
    found = db.get_visitor_by_participant_id(out["participant_id"], db_path=db_path)
    assert found["display_name"] == "Shari Vietry"


def test_the_kind_is_registered_on_the_rendezvous():
    """A kind that is not in all four registries is a request the operator can
    create and never decide, or decide and never have executed."""
    from tools.dashboard import approvals_routes as ap

    for registry in (ap.PREPARE_CREATE, ap.ENRICH,
                     ap.EXECUTORS, ap.AUTHORIZE_DECISION):
        assert va.KIND in registry
