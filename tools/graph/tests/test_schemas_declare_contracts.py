"""Every production Setting schema declares its payload's fields.

Declared fields are the contract. They are what ``validate_payload``
enforces, what ``graph set schema`` prints, what the exported JSON schema
and the generated TypeScript are built from. A schema declaring none
publishes nothing any of those can use, and enforcement has nothing to
enforce — the payload's shape then lives only in whatever its ``validate``
method happens to check, where no consumer can read it.

This is a check over the LIVE REGISTRY rather than a guard in
``__init_subclass__``. Tests legitimately define minimal throwaway schemas
with no fields, and ``test_no_annotations_is_fine`` asserts that stays
allowed; enforcing at construction breaks that contract and ~135 test
schemas with it. The guarantee that matters is about schemas the product
ships, and that is exactly what this asserts.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _import_all_production_schema_modules() -> None:
    for path in list((REPO_ROOT / "tools").rglob("*.py")) + list(
        (REPO_ROOT / "agents").rglob("*.py")
    ):
        s = str(path)
        if "/tests/" in s or path.name.startswith("test_"):
            continue
        try:
            if "SettingSchema)" not in path.read_text(errors="ignore"):
                continue
        except OSError:
            continue
        module = str(path.relative_to(REPO_ROOT))[:-3].replace("/", ".")
        try:
            importlib.import_module(module)
        except Exception:  # pragma: no cover - a module that cannot import
            continue        # is another test's problem, not this one's


@pytest.fixture(scope="module")
def registered_schemas():
    from tools.graph.schemas import registry as R

    _import_all_production_schema_modules()
    out = []
    for set_id in R.list_registered_set_ids():
        for revision in range(1, 12):
            cls = R.get_schema(set_id, revision)
            if cls is None:
                continue
            # The registry is process-global and other tests register throwaway
            # schemas into it. Only schemas defined in production modules are
            # shipped contracts; a class defined inside a test module is not.
            module = cls.__module__ or ""
            if ".tests." in module or module.rsplit(".", 1)[-1].startswith("test_"):
                continue
            out.append((set_id, revision, cls))
    assert out, "no schemas registered — the import sweep found nothing"
    return out


def test_every_schema_declares_at_least_one_field(registered_schemas):
    undeclared = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if not (getattr(cls, "_field_metadata", None) or {})
    ]
    assert not undeclared, (
        "these schemas declare no fields, so nothing can enforce, print or "
        "generate from their payload shape:\n  " + "\n  ".join(undeclared)
    )


def test_declared_types_are_recognised(registered_schemas):
    """A field's declared type must be one the enforcement layer understands.

    An unrecognised type name is not inert: ``enforce_declared_fields`` skips
    the check for it, so the field silently loses its type constraint. The
    fallback that produced ``"string"`` for a generic and for ``Any`` is the
    same failure wearing a different hat.
    """
    from tools.graph.schemas.registry import _JSON_TYPE_TO_PY

    known = set(_JSON_TYPE_TO_PY) | {"any"}
    bad = [
        f"{set_id}#{revision} {name}: {spec.get('type')!r}"
        for set_id, revision, cls in registered_schemas
        for name, spec in (getattr(cls, "_field_metadata", None) or {}).items()
        if spec.get("type") not in known
    ]
    assert not bad, "unrecognised declared type(s):\n  " + "\n  ".join(bad)


def test_every_declared_field_has_a_description(registered_schemas):
    """A declared field is a published contract; this is where it says what it means.

    It appears in ``graph set schema``, in the exported JSON schema and in the
    generated TypeScript, and the description is the only thing there telling a
    reader what the field is for. A field name is rarely self-explanatory to
    someone who did not write it.

    Asserted here rather than raised from ``field()`` for the same reason as the
    check above: a throwaway schema in a test publishes no contract, and
    enforcing at construction breaks those without protecting anything shipped.
    """
    undescribed = [
        f"{set_id}#{revision} {name}"
        for set_id, revision, cls in registered_schemas
        for name, spec in (getattr(cls, "_field_metadata", None) or {}).items()
        if not str(spec.get("description") or "").strip()
    ]
    assert not undescribed, (
        "these declared fields state no meaning, so nothing that renders the "
        "schema can explain them:\n  " + "\n  ".join(undescribed)
    )


def test_every_schema_declares_its_cardinality(registered_schemas):
    """How many rows exist at once is the first thing a schema must answer.

    The access-pattern decorator is that answer, and it is what tells a reader
    -- and codegen -- whether to expect one row, one per entity, or an
    append-only stream. Undeclared, the question was simply never asked, and
    the key strategy that follows from it cannot have been chosen either.

    A typed payload contract that is not a Setting row declares
    ``internal = True`` and stays out of the registry, so it is not asked a
    question it cannot answer.
    """
    undeclared = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if getattr(cls, "_access_pattern", None) is None
    ]
    assert not undeclared, (
        "these schemas declare no cardinality, so nothing states how many "
        "rows they have or what their key means:\n  " + "\n  ".join(undeclared)
    )


def test_internal_schemas_stay_out_of_the_registry(registered_schemas):
    """``internal = True`` means a payload contract, not a stored Setting.

    Anything walking the registry treats what it finds as a Setting -- the
    schema-meta flush writes every registered schema into every org database
    as a row. A payload shape that only borrows the field metadata must not
    be swept up in that.
    """
    leaked = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if getattr(cls, "internal", False)
    ]
    assert not leaked, (
        "these are marked internal but registered as Settings:\n  "
        + "\n  ".join(leaked)
    )


def test_no_schema_leaves_its_entity_unnamed(registered_schemas):
    """``natural`` means "the caller picks", which asserts nothing.

    A per-entity schema whose entity is unnamed has not decided its
    cardinality: the key strategy follows from the entity, and if nobody can
    say what the entity is, nobody chose the key either. Naming it is what
    makes the declaration carry information — and what lets a reader tell
    ``workspace_id`` from ``session_name`` without reading the writers.

    A genuinely caller-chosen key is legitimate, but it has to be stated:
    pass ``key_strategy="natural"`` explicitly and say why in the docstring,
    rather than taking it by default.
    """
    unnamed = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if getattr(cls, "_key_strategy", None) == "natural"
    ]
    assert not unnamed, (
        "these declare a per-entity key without naming the entity:\n  "
        + "\n  ".join(unnamed)
    )


# Schemas whose payload repeats a segment of their own key, grandfathered.
#
# The rule is that the key is returned with the row -- ``resolve_set_key``
# gives the base row including ``key``, every ``read_set`` member carries
# ``.key`` -- so repeating it in the payload is redundant and can drift: when
# the two disagree, nothing says which wins.
#
# These predate the rule and cannot be fixed by editing a schema. The
# duplicated fields are read at roughly a hundred production sites between
# them (``design_id`` 54, ``participant_id`` 38, ``credential_id`` 13), so
# removing one is a consumer migration, not a declaration change. Each is
# recorded here rather than silently skipped, so the debt is countable and a
# NEW collision still fails.
#
# ``autonomy.network.persona`` was on this list and is not: its duplication
# was removed rather than excused. Its schema claimed ``genesis_id`` was a
# derivation input that a payload read alone must still state -- but no
# consumer read it from the payload, the reader looks the row up BY that
# value, and the writer passed the same variable as key and field in one
# call. Zero rows existed, so removing it cost nothing. A duplicated
# derivation input is worse than a duplicated label, not better: if the two
# ever disagree you derive against the wrong org.
_KEY_DUPLICATION_GRANDFATHERED = {
    ("autonomy.identity.passkey", 1, "credential_id"),
    ("autonomy.network.ledger-projection", 1, "projection"),
    ("autonomy.network.ledger-state", 1, "genesis_id"),
    ("dashboard.harness.usage", 1, "harness"),
    ("dashboard.harness.usage", 1, "identity_id"),
    ("dashboard.plugin-owned-setting", 1, "plugin_id"),
    ("dashboard.plugin-owned-setting", 1, "set_id"),
    ("dashboard.presentation.deck", 1, "design_id"),
    ("dashboard.surface.presence", 1, "surface_id"),
    ("dashboard.surface.presence", 1, "participant_id"),
}


def test_no_new_schema_repeats_its_own_key_in_the_payload(registered_schemas):
    """The key comes back with the row; repeating it invites drift.

    Only strategies that name an entity are checked. ``fixed:``, ``uuid_v4``
    and ``natural`` name no segment to collide with.
    """
    import re

    unexpected = []
    for set_id, revision, cls in registered_schemas:
        strategy = getattr(cls, "_key_strategy", None) or ""
        if (not strategy or strategy in ("natural", "uuid_v4", "snowflake_id")
                or strategy.startswith("fixed:")):
            continue
        segments = [seg.strip("[]") for seg in re.split(r"[:/]", strategy)]
        fields = set(getattr(cls, "_field_metadata", None) or {})
        for segment in segments:
            if segment in fields and (set_id, revision, segment) not in _KEY_DUPLICATION_GRANDFATHERED:
                unexpected.append(f"{set_id}#{revision} field {segment!r} repeats its key")
    assert not unexpected, (
        "payload repeats a key segment; the key is already returned with the "
        "row:\n  " + "\n  ".join(unexpected)
    )

# Schemas registered before ``home`` existed, and not yet put through the
# decision.
#
# Which database a Setting lives in is not something to leave to whichever
# org a caller happened to pass: the operator's own store holds their
# identity and their credentials, an organization's store is what federates,
# and a value in the wrong one is either invisible to everyone who needs it
# or visible to everyone who should not have it.
#
# Every entry here is a set_id whose home nobody has stated yet. Deciding one
# is a re-home, not an edit: the declaration has to match where the rows
# actually are, or the assertion in the resolver starts refusing live reads.
# So they are listed rather than skipped -- the debt is countable, it shrinks
# by one line per set_id decided, and a NEW schema still cannot be registered
# without saying.
_HOME_UNDECLARED_GRANDFATHERED = {
    "autonomy.artifact-path",
    "autonomy.capability.contract",
    "autonomy.capability.impl",
    "autonomy.capability.operation_policy",
    "autonomy.commit.policy",
    "autonomy.commit.signing-key",
    "autonomy.harness.bootstrap",
    "autonomy.identity.passkey",
    "autonomy.identity.personal",
    "autonomy.network.binding",
    "autonomy.network.ledger-projection",
    "autonomy.network.ledger-state",
    "autonomy.network.link-grant",
    "autonomy.network.org-key",
    "autonomy.network.persona",
    "autonomy.network.serve-cert",
    "autonomy.org",
    "autonomy.org.bootstrap-allowlist",
    "autonomy.org.capability.install",
    "autonomy.org.capability.primer",
    "autonomy.org.peer-subscription",
    "autonomy.org.primer",
    "autonomy.secure.setting",
    "autonomy.source_control.review_state",
    "autonomy.workspace",
    "autonomy.workspace.artifact",
    "autonomy.workspace.capability.enable",
    "autonomy.workspace.mount",
    "autonomy.workspace.primer",
    "autonomy.workspace.turn_correction",
    "autonomy.worktree.review_binding",
    "dashboard.activity.ask",
    "dashboard.activity.ask_refresh",
    "dashboard.activity.ask_vote",
    "dashboard.activity.operator_dismissed",
    "dashboard.agent-actions",
    "dashboard.capability.host_install_state",
    "dashboard.claude.credentials",
    "dashboard.claude.setup_tokens",
    "dashboard.codex.credentials",
    "dashboard.coordinator",
    "dashboard.coordinator-bead",
    "dashboard.coordinator-canvas",
    "dashboard.coordinator-convergent-decision",
    "dashboard.coordinator-decision",
    "dashboard.coordinator-docs",
    "dashboard.coordinator-open-followup",
    "dashboard.coordinator-sprint",
    "dashboard.coordinator-thread",
    "dashboard.coordinator-tile",
    "dashboard.feature_flags",
    "dashboard.harness.usage",
    "dashboard.nexus.scene",
    "dashboard.nexus.tile",
    "dashboard.operator-message-to-coordinator",
    "dashboard.operator.activity",
    "dashboard.plugin",
    "dashboard.plugin-owned-setting",
    "dashboard.presentation.deck",
    "dashboard.session.crosstalk.worktree.rebase",
    "dashboard.session.orientation",
    "dashboard.session.upload",
    "dashboard.session.worktree.rebase_status",
    "dashboard.surface.ping",
    "dashboard.surface.presence",
    "dashboard.voice.transcription",
    "dashboard.worktree.terminal_fire",
    "dashboard.worktree.watch",
}


def test_a_new_schema_states_which_database_it_lives_in(registered_schemas):
    """The one thing a Setting cannot be silent about is where it is.

    Not defaulted, because there is no answer that is right often enough to
    be worth guessing: getting it wrong in one direction hides a value from
    everyone who needs it, and in the other direction shows it to everyone
    who should not have it.
    """
    from tools.graph.schemas.registry import declared_home

    undeclared = sorted(
        {
            set_id
            for set_id, _revision, _cls in registered_schemas
            if declared_home(set_id) is None
            and set_id not in _HOME_UNDECLARED_GRANDFATHERED
        }
    )
    assert not undeclared, (
        "these schemas do not say which database they live in; add "
        "``@home(...)`` above the access-pattern decorator:\n  "
        + "\n  ".join(undeclared)
    )


def test_the_grandfathered_list_does_not_outlive_its_entries(registered_schemas):
    """A set_id that HAS been decided must be struck from the list.

    Otherwise the list stops measuring anything: it would keep counting
    settled schemas as debt, and the number would never come down even as
    the work got done.
    """
    from tools.graph.schemas.registry import declared_home

    settled = sorted(
        set_id
        for set_id in _HOME_UNDECLARED_GRANDFATHERED
        if declared_home(set_id) is not None
    )
    assert not settled, (
        "these now declare a home and must be removed from "
        "_HOME_UNDECLARED_GRANDFATHERED:\n  " + "\n  ".join(settled)
    )
