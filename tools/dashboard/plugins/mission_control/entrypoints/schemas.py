"""Setting schemas owned by Mission Control's structured style.

A ``structured`` mission stores its content as one Settings row per item
instead of coordinator-pushed HTML documents. The platform's standard
viewer template renders the resolved rows client-side; coordinators write
items with ``graph set add``/``upsert`` and never author page scaffolding.

The item vocabulary below is not invented — it is the convergent shape all
six pillars of the first structured-candidate mission independently
arrived at in free-form HTML (scope / work / decisions / open questions /
where-it-stands, each claim graded proven-vs-code-only). The schema
ratifies that convention so every mission renders it the same way.
"""
from __future__ import annotations

from tools.graph.schemas.registry import (
    publication_band,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)


MISSION_ITEM_SET_ID = "dashboard.mission.item"
SCHEMA_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Structured Mission Control content: one row per mission/pillar item "
        "(scope, work, decision, question, status, metric, incident, exhibit), "
        "rendered by the platform's standard mission viewer."
    ),
    "nouns": [
        "mission", "mission control", "pillar", "mission item",
        "structured mission", "mission decision", "mission question",
        "mission status", "where it stands",
    ],
    "related_set_ids": [],
}

#: What an item IS. Decides which renderer draws it and which canonical
#: section it lands in when the item declares no section of its own.
ITEM_KINDS = (
    "scope",       # objective / owns / does-not-own
    "work",        # a unit of work with a proof-graded state
    "checkpoint",  # an ordered gate in an arc (work with sequence semantics)
    "decision",    # fork taken: chosen option, rationale, failure clause
    "question",    # open question; ``ask`` carries an operator-answerable ask
    "status",      # "where it stands" narrative (typically one per surface)
    "metric",      # a headline number with a label
    "incident",    # correction / negative result / incident record
    "exhibit",     # trusted HTML fragment for content that earns bespoke form
)

#: Where an item stands. The union of the state vocabularies the six
#: free-form pillars each reinvented (PROVEN/CODE ONLY, DONE/HALF DONE,
#: live defect/partly built/specified, ...), collapsed to one list so a
#: state renders identically on every mission.
ITEM_STATES = (
    "proven",     # exercised on the real running system, witnessed
    "done",       # complete (non-proof-graded work)
    "settled",    # decision made / question resolved
    "active",     # being worked now
    "code_only",  # tests pass; never run for real
    "next",       # sequenced, not started
    "specified",  # designed in full, not built
    "open",       # awaiting an answer or decision
    "blocked",    # cannot proceed; body says on what and who owns it
    "deferred",   # deliberately parked
    "retired",    # withdrawn; kept for the record, hidden by default
)


@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="surface_id:item_id")
class MissionItemV1(SettingSchema):
    """One item on a structured mission screen.

    Key: ``<surface_id>:<item_id>`` where ``surface_id`` is the mission id
    (overview items) or a pillar id, and ``item_id`` is a caller-chosen
    stable slug. Questions anchor to items by this key, so renaming an
    ``item_id`` is the same contract violation as renaming a
    ``data-mc-anchor`` on a free-form screen: don't.
    """

    set_id = MISSION_ITEM_SET_ID
    schema_revision = SCHEMA_REVISION

    # surface_id/item_id live in the composite KEY, never the payload
    # (settings doctrine; Central schema gate). Historical rows still
    # carry them as undeclared extras — readers derive from the key and
    # tolerate the leftovers.
    kind: str = field(
        required=True, enum=list(ITEM_KINDS),
        description="What the item is; picks the renderer and default section")
    state: str = field(
        default="active", enum=list(ITEM_STATES),
        description="Where the item stands, in the platform-wide vocabulary")
    title: str = field(
        required=True,
        description="Plain-language headline, readable outside the repository")
    body: str = field(
        default="",
        description="Supporting prose; plain text, blank line = paragraph break")
    section: str = field(
        default="",
        description="Optional grouping label; blank derives the canonical "
                    "section from kind")
    order: float = field(
        default=0.0,
        description="Sort key within a section; ties break on title")
    evidence: list = field(
        default_factory=list, element=str,
        description="Evidence sentences: what was exercised and what was seen")
    refs: list = field(
        default_factory=list, element=str,
        description="Provenance refs, prefixed: commit:<sha> bead:<id> "
                    "graph:<id> rev:<n>")
    ask: str = field(
        default="",
        description="Operator-answerable ask text (question kind, open state); "
                    "one question, names its options, states the consequence")
    fork: str = field(
        default="",
        description="Decision kind: the fork that was faced")
    chosen: str = field(
        default="",
        description="Decision kind: the option taken and why")
    if_wrong: str = field(
        default="",
        description="Decision kind: what fails if the choice proves wrong")
    value: str = field(
        default="",
        description="Metric kind: the headline figure, units included")
    html: str = field(
        default="",
        description="Exhibit kind: trusted self-contained HTML fragment; "
                    "same trust level as a free-form screen push")
    happened_at: str = field(
        default="",
        description="ISO-8601 moment the state was earned in the world, when "
                    "known; blank falls back to the row's updated_at")
    owner: str = field(
        default="",
        description="Owning session or pillar name, for cross-surface rows")
