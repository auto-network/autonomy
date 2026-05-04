"""Action handlers for the coordinator-board plugin.

Each handler is a registered callback the settings-mediator substrate
(:mod:`tools.dashboard.settings_mediator`) invokes when a new row lands
in one of the two work-queue Settings owned by the coordinator board:

* ``dashboard.coordinator-decision`` — append-only event log of operator
  taps on a coordinator tile (thumb yes/no, choice, custom reply, sitrep
  request, refresh request). One handler per ``kind`` synthesizes the
  text the coordinator session receives via tmux.
* ``dashboard.operator-message-to-coordinator`` — operator → coordinator
  free text. The handler resolves the bound session from the
  ``dashboard.coordinator`` singleton Setting and forwards the body
  verbatim. No binding resolved → log + drop.

Module-level ``register_action_decorator`` calls fire at import time —
the plugin loader imports this module via ``entrypoints.actions`` in
``plugin.yaml``, which is what wires the handlers into the registry.

Idempotency, restart-resume, and per-handler failure isolation are all
substrate concerns; the handlers themselves are stateless one-liners.
"""
from __future__ import annotations

import json

from tools.dashboard.settings_mediator import register_action_decorator
from tools.graph import settings_ops


COORDINATOR_SET_ID = "dashboard.coordinator"
COORDINATOR_DECISION_SET_ID = "dashboard.coordinator-decision"
OPERATOR_MESSAGE_SET_ID = "dashboard.operator-message-to-coordinator"
COORDINATOR_ORG = "autonomy"


def _bound_coordinator_session() -> str | None:
    binding = settings_ops.resolve_set_key(
        COORDINATOR_SET_ID,
        "default",
        org=COORDINATOR_ORG,
        peers=[],
    )
    if not binding:
        return None
    payload = binding.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        payload = {}
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return None


# ── coordinator-decision handlers ────────────────────────────────────


@register_action_decorator(
    COORDINATOR_DECISION_SET_ID,
    predicate=lambda r: r.get("kind") == "thumb_yes",
    name="coordinator_board.thumb_yes",
)
async def thumb_yes(row, svc):
    await svc.session_send(
        row["target_session"],
        f"Operator: thumb yes on {row['tile_id']}.",
    )


@register_action_decorator(
    COORDINATOR_DECISION_SET_ID,
    predicate=lambda r: r.get("kind") == "thumb_no",
    name="coordinator_board.thumb_no",
)
async def thumb_no(row, svc):
    await svc.session_send(
        row["target_session"],
        f"Operator: thumb no on {row['tile_id']}.",
    )


@register_action_decorator(
    COORDINATOR_DECISION_SET_ID,
    predicate=lambda r: r.get("kind") == "choice",
    name="coordinator_board.choice",
)
async def choice(row, svc):
    await svc.session_send(
        row["target_session"],
        f"Operator chose: {row['choice']} on {row['tile_id']}.",
    )


@register_action_decorator(
    COORDINATOR_DECISION_SET_ID,
    predicate=lambda r: r.get("kind") == "custom",
    name="coordinator_board.custom_reply",
)
async def custom_reply(row, svc):
    # Operator-authored free text; pass through unmodified.
    await svc.session_send(row["target_session"], row["choice"])


@register_action_decorator(
    COORDINATOR_DECISION_SET_ID,
    predicate=lambda r: r.get("kind") == "sitrep_request",
    name="coordinator_board.sitrep_request",
)
async def sitrep_request(row, svc):
    await svc.session_send(
        row["target_session"],
        f"Operator requests a sitrep on {row['tile_id']}.",
    )


@register_action_decorator(
    COORDINATOR_DECISION_SET_ID,
    predicate=lambda r: r.get("kind") == "refresh_request",
    name="coordinator_board.refresh_request",
)
async def refresh_request(row, svc):
    await svc.session_send(
        row["target_session"],
        f"Operator requests a refresh on {row['tile_id']}.",
    )


# ── operator-message-to-coordinator handler ──────────────────────────


@register_action_decorator(
    OPERATOR_MESSAGE_SET_ID,
    name="coordinator_board.operator_message",
)
async def operator_message(row, svc):
    target = _bound_coordinator_session()
    if not target:
        svc.log.warning(
            "coordinator_board.operator_message: no dashboard.coordinator "
            "session resolved; dropping row %s",
            row.id,
        )
        return
    await svc.session_send(target, row["text"])
