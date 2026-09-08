"""Setting schemas owned by the ``mission`` plugin.

Everything the plugin renders is settings-backed: mission records,
pillar records, content items, and the per-pillar chat log. Tasks are
deliberately NOT here — they are beads, read from bd at render time and
never duplicated into Settings (the single-source-of-truth decision on
the design-of-record note, graph://ed8d8b50-418).

Designed per How to Write a Setting (graph://d72e25ec-a41):
SCOPE — every field is the organization's shared fact (which missions
exist, what a pillar owns, what has been delivered); all four sets are
org-homed. CARDINALITY — one row per named entity in every set.
KEY — the handle callers already hold: mission ids appear in bead labels
(``mission:<uuid>``), pillar slugs in ``pillar:<slug>`` labels; composite
keys are ``<mission_id>:<slug>``. Key segments are NOT repeated as
payload fields — readers recover them from the member key.
PUBLICATION — raw→curated: mission content reaches visitors through the
plugin's own server-side rendering, never through federated reads.

The item vocabulary is schema v2, greenfield (no compatibility with
``dashboard.mission.item``): a state exists only where it means
something, and accountability is event streams on the item, not a
version trail in the store.
"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit

from tools.graph.schemas.registry import (
    append_only_log,
    publication_band,
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)


MISSION_SET_ID = "mission.registry"
PILLAR_SET_ID = "mission.pillar"
ITEM_SET_ID = "mission.item"
CHAT_SET_ID = "mission.chat"
SCHEMA_REVISION = 1

#: What an item IS. Five kinds; tasks are beads, numbers are rich text.
ITEM_KINDS = (
    "scope",       # the charter: objective, completion condition, non-goals
    "status",      # one timestamped news update (a stream, newest first)
    "checkpoint",  # an acceptance criterion: demonstrable end-to-end value
    "decision",    # fork taken: chosen option, rationale, failure clause
    "question",    # a conversation, open until a cohesive answer resolves it
)

#: Per-kind state vocabularies. Stateless kinds carry the empty string.
CHECKPOINT_STATES = ("confirmed", "in_progress", "pending")
QUESTION_STATES = ("open", "answered")


def safe_artifact_href(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if re.search(r"[\x00-\x20\x7f\\]", value) or value.startswith("//"):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            return False
        if parsed.scheme in ("http", "https"):
            return bool(parsed.hostname)
        if parsed.scheme == "graph":
            return bool(re.fullmatch(r"graph://[a-zA-Z0-9-]+", value))
        return not parsed.scheme and bool(re.match(
            r"^/(?:design/|bead/|session/|mission/|api/session/[^/]+/output/)", value))
    except ValueError:
        return False


def valid_iso(value: str) -> bool:
    try:
        return isinstance(value, str) and len(value) <= 64 and \
            datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except (ValueError, TypeError):
        return False


def validate_briefing(value: dict) -> None:
    """A bounded display brief, never authority or a second task record."""
    caps = {"subtitle": 240, "recommendation": 4000, "impact": 4000,
            "generated_at": 64}
    allowed = set(caps) | {"options", "artifacts", "source_refs"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise SchemaValidationError("briefing contains unsupported fields")
    for key, limit in caps.items():
        if key in value and (not isinstance(value[key], str) or len(value[key]) > limit):
            raise SchemaValidationError(f"briefing {key} exceeds its string bound")
    if "generated_at" in value and not valid_iso(value["generated_at"]):
        raise SchemaValidationError("briefing generated_at requires an ISO timestamp with timezone")
    for key, count, fields, required in (
        ("options", 8, {"label": 160, "consequence": 1000, "text": 4000}, {"label", "text"}),
        ("artifacts", 12, {"label": 160, "href": 2048}, {"label", "href"}),
    ):
        entries = value.get(key, [])
        if not isinstance(entries, list) or len(entries) > count:
            raise SchemaValidationError(f"briefing {key} exceeds its list bound")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - set(fields) or not required <= set(entry):
                raise SchemaValidationError(f"briefing {key} entry has invalid fields")
            if any(not isinstance(v, str) or len(v) > fields[k] for k, v in entry.items()):
                raise SchemaValidationError(f"briefing {key} entry exceeds string bound")
            if key == "artifacts" and not safe_artifact_href(entry["href"]):
                raise SchemaValidationError("briefing artifact href is unsafe")
    refs = value.get("source_refs", [])
    if not isinstance(refs, list) or len(refs) > 40 or any(
        not isinstance(v, str) or len(v) > 2048 for v in refs
    ):
        raise SchemaValidationError("briefing source_refs exceeds its bound")

_ENTRY_ELEMENT = {
    "by": {"type": "string", "required": True,
           "description": "Author: session name or person label"},
    "at": {"type": "string", "required": True,
           "description": "ISO-8601 moment"},
    "text": {"type": "string", "required": True,
             "description": "Entry prose (markdown subset)"},
}


@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="mission_id")
class MissionV1(SettingSchema):
    """One mission. Key: the mission id (uuid) — the same identifier
    bead labels carry as ``mission:<uuid>``."""

    set_id = MISSION_SET_ID
    schema_revision = SCHEMA_REVISION

    name: str = field(
        required=True,
        description="Display name, readable outside the repository")
    status: str = field(
        default="active", enum=["active", "paused", "complete"],
        description="Explicit lifecycle; never inferred from activity")
    coordinator_session: str = field(
        default="",
        description="Session receiving mission-level chat and questions; "
                    "routing data, not a permission")
    status_changed_at: str = field(
        default="",
        description="ISO-8601 moment of the last lifecycle transition; "
                    "stamped by the status route, shown beside last "
                    "activity on the homepage")


@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="mission_id:pillar_id")
class MissionPillarV1(SettingSchema):
    """One pillar. Key: ``<mission_id>:<pillar_id>`` where the pillar id
    is a short stable slug (``relay``, ``crypto``)."""

    set_id = PILLAR_SET_ID
    schema_revision = SCHEMA_REVISION

    name: str = field(
        required=True,
        description="Display name, readable outside the repository")
    color: str = field(
        default="#8391a8",
        description="CSS color hint for the pillar band; no behavior")
    status: str = field(
        default="active", enum=["active", "paused", "complete"],
        description="Explicit lifecycle; never inferred from activity")
    coordinator_session: str = field(
        default="",
        description="Session receiving this pillar's chat and questions")
    order: float = field(
        default=0.0,
        description="Sort key across the mission's pillars")
    bead_labels: list = field(
        default_factory=list, element=str,
        description="bd pillar labels this pillar owns (e.g. "
                    "'pillar:relay-network'); the task bridge maps beads "
                    "to the pillar through these, so the organic bd "
                    "vocabulary never needs a mass retag")


@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="mission_id:pillar_id:item_id")
class MissionContentV1(SettingSchema):
    """One content item on a pillar (schema v2, greenfield).

    Key: ``<mission_id>:<pillar_id>:<item_id>``. Every item belongs to a
    pillar — the mission overview is a computed summary, never a content
    surface (the rule the legacy platform converged on). ``item_id`` is
    a caller-chosen stable slug; questions and anchors bind to it, so
    renaming one whose subject survives is a contract violation.

    States exist only where they mean something: checkpoints climb
    confirmed/in_progress/pending, questions are open/answered, and
    scope/status/decision carry no state at all. Accountability is two
    event streams appended in place — ``history`` (state transitions)
    and ``work`` (attributed progress) — because the settings store
    upserts rows whole and keeps no version trail of its own.
    """

    set_id = ITEM_SET_ID
    schema_revision = SCHEMA_REVISION

    kind: str = field(
        required=True, enum=list(ITEM_KINDS),
        description="What the item is; picks renderer, tab, and vocabulary")
    state: str = field(
        default="", enum=[""] + list(CHECKPOINT_STATES) + list(QUESTION_STATES),
        description="Per-kind state; empty for stateless kinds "
                    "(scope, status, decision)")
    title: str = field(
        required=True,
        description="Plain-language headline, readable outside the repo")
    body: str = field(
        default="",
        description="Supporting prose; markdown subset (headings, lists, "
                    "fences, links, graph://ids, images -> gallery)")
    briefing: dict = field(
        default_factory=dict,
        description="Optional Ops subtitle, recommendation, impact, options and artifact links; display advice, not authority")
    order: float = field(
        default=0.0,
        description="Explicit pin within a tab; 0 defers to the tab's "
                    "own rule (news: newest first; delivery: bead topology)")
    section: str = field(
        default="",
        description="Optional group heading within the item's tab "
                    "(e.g. a phase name); blank means ungrouped")
    evidence: list = field(
        default_factory=list,
        element={
            "text": {"type": "string", "required": True,
                     "description": "What was exercised and what was seen; "
                                    "markdown (figures render bold, images "
                                    "join the gallery)"},
            "at": {"type": "string",
                   "description": "ISO-8601 moment the evidence was earned"},
            "by": {"type": "string",
                   "description": "Witnessing session"},
            "turn": {"type": "integer",
                     "description": "Turn in the witnessing session"},
        },
        description="Checkpoint evidence entries, each with provenance")
    refs: list = field(
        default_factory=list, element=str,
        description="Provenance refs, prefixed: bead:<id> commit:<sha> "
                    "graph:<id>; a checkpoint's bead refs are its linked "
                    "tasks")
    ask: str = field(
        default="",
        description="Reader-directed ask text (question kind): one "
                    "answerable question naming options and consequence")
    blocking: bool = field(
        default=False,
        description="Question kind: this open question prevents forward "
                    "progress (task dependency order is sequencing, never "
                    "blockage)")
    asked_by: str = field(
        default="",
        description="Question kind: who asked (session name or label)")
    asked_at: str = field(
        default="",
        description="Question kind: ISO-8601 moment it was asked")
    discussion: list = field(
        default_factory=list,
        element={**_ENTRY_ELEMENT,
                 "type": {"type": "string",
                          "description": "'reply' (default) or 'progress' "
                                         "(transient status while working "
                                         "out the answer)"}},
        description="Question kind: the conversation — attributed replies "
                    "and progress entries; a reply is NOT an answer")
    answer: dict = field(
        default_factory=dict,
        description="Question kind: the one cohesive resolution "
                    "{text, by, at}; filing it closes the question")
    fork: str = field(
        default="",
        description="Decision kind: the fork that was faced")
    chosen: str = field(
        default="",
        description="Decision kind: the option taken and why")
    if_wrong: str = field(
        default="",
        description="Decision kind: what fails if the choice proves wrong")
    faq: bool = field(
        default=False,
        description="Decision kind: pinned as a favorite / FAQ decision")
    work: list = field(
        default_factory=list, element=_ENTRY_ELEMENT,
        description="Checkpoint kind: attributed progress entries appended "
                    "while a session works the criterion; 'actively worked' "
                    "is derived from recency, never stored")
    history: list = field(
        default_factory=list,
        element={
            "from": {"type": "string", "required": True,
                     "description": "State before"},
            "to": {"type": "string", "required": True,
                   "description": "State after"},
            "at": {"type": "string", "required": True,
                   "description": "ISO-8601 moment"},
            "by": {"type": "string", "required": True,
                   "description": "Session that made the transition"},
        },
        description="Checkpoint kind: every state transition, appended by "
                    "the state route — the trail the upserting store "
                    "cannot keep itself")
    confirmed_by: str = field(
        default="",
        description="Checkpoint kind: session that confirmed the "
                    "demonstration")
    confirmed_turn: int = field(
        default=0,
        description="Checkpoint kind: turn in the confirming session")
    confirmed_at: str = field(
        default="",
        description="Checkpoint kind: ISO-8601 moment of confirmation")
    happened_at: str = field(
        default="",
        description="ISO-8601 moment the item's fact was earned; blank "
                    "falls back to the row's updated_at")
    owner: str = field(
        default="",
        description="Owning session or pillar name, for cross-surface rows")
    retired: bool = field(
        default=False,
        description="Withdrawn; kept for the record, hidden from screens")

    @classmethod
    def validate(cls, payload: dict) -> None:
        """Per-kind rules a field declaration cannot express."""
        super().validate(payload)
        kind = payload.get("kind")
        if "briefing" in payload:
            if kind not in ("question", "checkpoint", "status"):
                raise SchemaValidationError("briefing belongs to question/checkpoint/status")
            validate_briefing(payload["briefing"])
        state = payload.get("state", "")
        if kind == "checkpoint":
            if state not in CHECKPOINT_STATES:
                raise SchemaValidationError(
                    f"checkpoint state must be one of {CHECKPOINT_STATES}, "
                    f"got {state!r}")
        elif kind == "question":
            if state not in QUESTION_STATES:
                raise SchemaValidationError(
                    f"question state must be one of {QUESTION_STATES}, "
                    f"got {state!r}")
            if payload.get("answer") and state != "answered":
                raise SchemaValidationError(
                    "a question carrying an answer must be state 'answered'")
        else:
            if state:
                raise SchemaValidationError(
                    f"{kind} items are stateless; got state {state!r}")
            if payload.get("blocking"):
                raise SchemaValidationError(
                    "blocking is a property of an open question")
        if kind != "decision" and payload.get("faq"):
            raise SchemaValidationError("faq marks decisions only")
        if kind != "checkpoint" and (payload.get("work")
                                     or payload.get("history")):
            raise SchemaValidationError(
                "work/history streams belong to checkpoints")


@publication_band(min="raw", max="curated")
@home("organization")
@append_only_log(key="mission_id:pillar_id:uuid")
class MissionChatV1(SettingSchema):
    """One chat MESSAGE. Key: ``<mission_id>:<pillar_id>:<uuid>``.

    One row per message, deliberately: when the signed-settings
    envelope lands (design 21a0da9e-1c2), each message individually
    carries its writer's signed membership identity — the substrate
    does attribution, nothing is stamped. Until then ``by`` records
    the writer: a member's org persona public key (resolved to their
    directory profile at render), or an agent's session name.

    A free conversation — the human's only input surface. Nothing here
    is tracked: no states, no categories. The pillar agent decides what
    a message warrants and writes that itself.
    """

    set_id = CHAT_SET_ID
    schema_revision = SCHEMA_REVISION

    by: str = field(
        required=True,
        description="Writer: org member persona public key (64 hex; "
                    "resolved via autonomy.org.member-profile at render) "
                    "or an agent session name. Superseded by the signed "
                    "envelope's terminal_persona once signing lands")
    at: str = field(
        required=True,
        description="ISO-8601 moment the message was sent")
    text: str = field(
        required=True,
        description="Message prose (markdown subset)")
