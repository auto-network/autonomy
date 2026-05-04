"""Substrate-native base for "ask agent to X" directives.

Every dashboard feature that boils down to *"operator clicks button →
setting write → mediator forwards body to a tmux session over CrossTalk"*
collapses to the same shape. Today these are one-off REST endpoints + JS
state machines (request-rebase, request-identity-refresh). The substrate
primitive is one schema family with a default delivery action.

:class:`CrosstalkDirective` is the canonical base. It establishes the
namespace root ``dashboard.session.crosstalk`` (no ``schema_revision`` —
the base is abstract), declares the
``target_session`` / ``body`` / ``sender`` field shape, and provides the
default :func:`deliver` action that forwards ``row['body']`` unchanged.
Concrete directives subclass it, set their :attr:`set_id_suffix`, and
either inherit :func:`deliver` (passthrough body) or override it with
``@action`` (server-side body rendering).

Add a new directive
-------------------

.. code-block:: python

    class RequestRebaseV1(CrosstalkDirective):
        set_id_suffix = "request-rebase"
        schema_revision = 1

The composed ``set_id`` is ``dashboard.session.crosstalk.request-rebase``
(see :func:`tools.graph.schemas.registry._compose_set_id_from_suffix`).
The inherited :func:`deliver` action fires on every write to that set
and pastes ``row['body']`` into ``row['target_session']`` via the
settings-mediator's :class:`Services.session_send` primitive — no
per-feature plumbing.

Override delivery
-----------------

When the directive needs server-side body rendering (e.g. inlining
metadata, formatting envelopes), redeclare ``deliver`` with ``@action``:

.. code-block:: python

    class RequestIdentityRefreshV1(CrosstalkDirective):
        set_id_suffix = "request-identity-refresh"
        schema_revision = 1

        @action
        async def deliver(row, svc):
            rendered = f"Refresh identity for {row['target_session']}"
            await svc.session_send(row['target_session'], rendered)

The override shadows the inherited handler — only the override fires for
the subclass's set_id. Re-applying ``@action`` is required: an unmarked
shadow disables the action entirely.

Payload conventions
-------------------

* ``target_session`` (required) — the tmux session to send to. Matches
  the existing CrosstalkService ``target=`` semantics.
* ``body`` (required) — the message body. The default :func:`deliver`
  forwards this unchanged; subclasses that override may treat ``body``
  as a structured input and render their own envelope.
* ``sender`` (default ``"dashboard-ui"``) — the operator-facing sender
  identity; lives in the row for audit / display, not interpreted by
  the default :func:`deliver`.

Access pattern
--------------

The base is decorated :func:`@append_only_log <tools.graph.schemas.registry.append_only_log>`:
each operator click is a distinct delivered message, and the log of
every operator-issued directive is intrinsic. Codegen-aware consumers
(JS proxy / generated typed methods) expose ``.append(payload)`` for
this set; the natural form on the Alpine side is::

    Schema.alpine(state, {schemas: {requestRebase: 'dashboard.session.crosstalk.request-rebase'}})
    // …
    state.requestRebase.append({target_session, body, sender})

Provenance
----------

* Bead: auto-ixzdz (this module).
* Bead C — ``set_id_suffix`` composition + collision detection
  (``auto-uqdkk``).
* Bead B — ``@action`` discovery on :class:`SettingSchema`
  (``auto-xyqla``).
* Substrate seams used: settings-mediator
  (:mod:`tools.dashboard.settings_mediator`),
  ``setting.changed`` event bus (auto-5mz65), :class:`Services` wiring
  in ``server._build_settings_mediator_services``.
"""
from __future__ import annotations

from tools.graph.schemas.registry import (
    SettingSchema,
    action,
    append_only_log,
    field,
)


SYNOPSIS = {
    "summary": (
        "Substrate-native family for 'operator clicks button → tmux "
        "paste over CrossTalk' directives. Concrete directives subclass "
        "CrosstalkDirective with set_id_suffix; the inherited deliver "
        "action forwards row['body'] to row['target_session'] via "
        "Services.session_send."
    ),
    "nouns": [
        "crosstalk directive", "operator directive", "session directive",
        "tmux paste", "crosstalk forward", "request-rebase",
        "request-identity-refresh",
    ],
    "related_set_ids": [],
}


CROSSTALK_DIRECTIVE_NAMESPACE = "dashboard.session.crosstalk"


@append_only_log
class CrosstalkDirective(SettingSchema):
    """Abstract base for the CrosstalkDirective family.

    Subclasses declare ``set_id_suffix`` and ``schema_revision`` to
    compose a concrete leaf set_id under
    :data:`CROSSTALK_DIRECTIVE_NAMESPACE`. The inherited :func:`deliver`
    action runs for every write to the leaf set unless the subclass
    overrides it with ``@action``.

    The base itself has no ``schema_revision`` and so is not registered
    as a concrete schema in :data:`tools.graph.schemas.registry.SCHEMAS`
    — its role is to establish the namespace root and provide field
    shape + default action for descendants.
    """

    set_id = CROSSTALK_DIRECTIVE_NAMESPACE

    target_session: str = field(
        required=True,
        description=(
            "The tmux session to deliver into. Matches "
            "Services.session_send(target=…) semantics."
        ),
    )
    body: str = field(
        required=True,
        description=(
            "The message body. Default deliver() forwards verbatim; "
            "subclasses that override may treat this as structured "
            "input."
        ),
    )
    sender: str = field(
        default="dashboard-ui",
        description=(
            "Operator-facing sender identity, recorded for audit / "
            "display. Not interpreted by default deliver()."
        ),
    )

    @action
    async def deliver(row, svc):
        """Default delivery: forward ``row['body']`` to ``row['target_session']``.

        Subclasses inherit this handler unmodified by leaving ``deliver``
        undeclared on the subclass; subclasses that need server-side
        rendering redeclare ``deliver`` with ``@action`` and an unmarked
        override silently disables the action for that subclass.
        """
        await svc.session_send(row["target_session"], row["body"])

    def __init_subclass__(cls, **kwargs):
        """Register inherited ``@action`` / ``@on_kind`` markers under
        the subclass's composed ``set_id``.

        :class:`SettingSchema`'s ``__init_subclass__`` only walks
        ``cls.__dict__`` when discovering action markers, so an action
        declared on :class:`CrosstalkDirective` does not auto-register
        for descendants. This hook fills the gap: after the substrate
        finishes its per-class work (``super().__init_subclass__`` →
        ``_compose_set_id_from_suffix`` + ``_auto_register_schema`` +
        ``_discover_actions``), we walk the MRO and register every
        inherited marked method whose name was not shadowed by the
        subclass's own ``cls.__dict__``.

        Shadowing rule: any callable attribute declared on the subclass
        (marked or not) suppresses inherited markers of the same name.
        Re-applying ``@action`` on the override is therefore required to
        keep the action active; an unmarked shadow silently disables it.

        Abstract intermediates (no ``schema_revision`` of their own)
        skip the inheritance pass — they're namespace-only and their
        actions exist for concrete descendants to inherit, not to fire
        themselves.
        """
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("schema_revision"):
            return
        cls_set_id = cls.__dict__.get("set_id") or getattr(cls, "set_id", None)
        if not cls_set_id:
            return
        from tools.dashboard.settings_mediator import register_action_decorator

        seen: set[str] = set()
        for attr_name, value in cls.__dict__.items():
            if callable(value):
                seen.add(attr_name)
        for ancestor in cls.__mro__[1:]:
            if ancestor is object:
                continue
            for attr_name, value in ancestor.__dict__.items():
                if attr_name in seen:
                    continue
                if not callable(value):
                    continue
                seen.add(attr_name)
                registered_name = f"{cls.__name__}.{attr_name}"
                if getattr(value, "_is_substrate_action", False):
                    register_action_decorator(
                        cls_set_id, name=registered_name,
                    )(value)
                elif hasattr(value, "_action_kind"):
                    kind = value._action_kind
                    register_action_decorator(
                        cls_set_id,
                        predicate=(lambda r, k=kind: r.get("kind") == k),
                        name=registered_name,
                    )(value)
