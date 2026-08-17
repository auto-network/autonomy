"""Admitting a person to the missions, as an approval the operator decides.

Handing someone a way in is the operator's to allow, so the route that mints
one is closed to every session. That left the one thing only the operator can
do as the one thing they had no way to do except by typing a command with a
photo encoded into it -- which is not something anybody does from a phone.

This is the same request the route takes, carried on the approval rendezvous
instead: an agent asks, the operator sees who is being let in and taps once,
and the token comes back to the agent that asked. No wider credential, no
second way to mint a guest, and nothing here bypasses the guard -- the
operator's decision IS the authority, and it is checked the same way every
other approval checks it.

The token is a secret and is returned exactly once, so it travels in the
result of the request that asked for it and is stored nowhere else.
"""

from __future__ import annotations

import re

from starlette.requests import Request


KIND = "visitor_token"

#: A person's name, as it will appear beside everything they ever ask.
_MAX_NAME = 120

#: Matches the route's own rule: the bytes arrive as a data URL and are put in
#: the attachment store once, never on the visitor row.
_AVATAR_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,")


def prepare_create(session: str, request: dict) -> tuple[dict, dict]:
    """Check what the operator is about to be asked to allow.

    Refuses here rather than at execution so a request that could never
    succeed never reaches the operator as something to decide.
    """
    unknown = set(request) - {"display_name", "avatar", "reason"}
    if unknown:
        raise ValueError(
            "a visitor request carries display_name, an optional avatar and an "
            f"optional reason; got also: {', '.join(sorted(unknown))}"
        )
    name = (request.get("display_name") or "").strip()
    if not name:
        raise ValueError("display_name is required: it is how this person is "
                         "named beside everything they ask")
    if len(name) > _MAX_NAME:
        raise ValueError(f"display_name must be at most {_MAX_NAME} characters")

    avatar = request.get("avatar")
    if avatar not in (None, "") and (
        not isinstance(avatar, str) or not _AVATAR_RE.match(avatar)
    ):
        raise ValueError(
            "avatar must be a data:image/<png|jpeg|gif|webp>;base64 URL"
        )

    reason = (request.get("reason") or "").strip()

    # The photo is put in the attachment store HERE, not on approval, so that
    # what the operator is shown is a face and not a note saying a face was
    # attached. Seeing who is being admitted is the decision.
    #
    # Storing it before the answer is known means a declined request leaves an
    # image behind. That store is content-addressed and de-duplicates, so the
    # cost is one copy of a photo that was already sent, and the alternative
    # is deciding blind.
    attachment_id = None
    if avatar:
        from tools.dashboard.plugins.mission_control.entrypoints import api as mc

        attachment_id, error = mc._store_avatar(avatar, name)
        if error:
            raise ValueError(error)

    stored = {"display_name": name, "reason": reason}
    if attachment_id:
        stored["avatar_attachment_id"] = attachment_id
    staged = {
        "display_name": name,
        "reason": reason,
        "asked_by": session,
        "avatar_attachment_id": attachment_id,
        # Same route every other avatar in the dashboard is drawn from.
        "avatar_url": f"/api/attachment/{attachment_id}" if attachment_id else None,
    }
    return stored, staged


def enrich(row: dict) -> dict:
    """What the operator sees before deciding: who, why, and who asked."""
    return {"staged": row.get("staged")}


def authorize_decision(request: Request, _row: dict, _decision: dict) -> str | None:
    """Only a human-origin operator session may allow somebody in.

    Without this any caller that could reach the decision route could admit
    a guest, which would make the approval a formality rather than a gate.
    """
    from tools.dashboard import unlock_routes

    if unlock_routes.gate_disabled():
        return None
    session = unlock_routes.session_from_request(request)
    if session is None or session.get("method") not in {
        "bootstrap", "passkey", "password",
    }:
        return "unlock the dashboard before deciding who is admitted"
    return None


async def execute(row: dict, decision: dict) -> dict:
    """Mint the way in, and hand it back to whoever asked for it."""
    if not decision.get("approved"):
        return {"ok": False, "error": "not approved"}

    # Imported here: this module is loaded by the approvals routes at import
    # time, and the plugin brings the whole Mission Control surface with it.
    from tools.dashboard.dao import mission_control_db as db

    request = row.get("request") or {}
    name = (request.get("display_name") or "").strip()
    if not name:
        return {"ok": False, "error": "the request no longer carries a name"}

    # The photo was stored when the request was made, so the operator could
    # see it. Nothing to do here but name it.
    attachment_id = request.get("avatar_attachment_id")

    visitor = db.create_visitor_token(name, avatar_attachment_id=attachment_id)
    return {
        "ok": True,
        # The secret, travelling once, to the session that asked.
        "token": visitor["token"],
        "participant_id": visitor["participant_id"],
        "display_name": visitor["display_name"],
        "avatar_attachment_id": visitor.get("avatar_attachment_id"),
    }


PREPARE_CREATE = {KIND: prepare_create}
ENRICH = {KIND: enrich}
EXECUTORS = {KIND: execute}
AUTHORIZE_DECISION = {KIND: authorize_decision}
