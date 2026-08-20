"""Which organization the dashboard shell renders by default, on this node.

A fact about THIS installation — the org whose data non-plugin pages show
when nothing narrower chose one — so it is machine-homed: declared once,
inspectable, allowed to differ per node, and never synced. It replaces the
last ambient answer to that question (the dashboard process's ``GRAPH_ORG``
environment, deleted with the rest of the default-scope infrastructure):
scope has three sources — the session credential, an explicit selection, or
a declaration — and this is the declaration for the shell's rendering
default. It is a UI default only: it carries no authority, and no request
handler may consult it to decide a caller's scope.

Seeded by first-run from the installation's first organization; the shell
falls back to the first listed organization on nodes initialized before the
seed existed.
"""

from __future__ import annotations

from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)

SHELL_DEFAULT_ORG_SET_ID = "dashboard.shell.default-org"
SHELL_DEFAULT_ORG_REVISION = 1
SHELL_DEFAULT_ORG_KEY = "default"

SYNOPSIS = {
    "summary": (
        "The org the dashboard shell renders by default on this node — a "
        "machine-homed UI declaration, seeded by first-run, carrying no "
        "authority. The deletion target it replaced was the dashboard "
        "process's GRAPH_ORG environment."
    ),
    "nouns": [
        "shell org", "default org", "dashboard shell", "node default",
    ],
    "related_set_ids": ["autonomy.machine.identity#1"],
}


@home("machine")
@publication_band(max="raw")
@singleton(key=SHELL_DEFAULT_ORG_KEY)
class DashboardShellDefaultOrgV1(SettingSchema):
    """The one row naming the shell's default organization for this node."""

    set_id = SHELL_DEFAULT_ORG_SET_ID
    schema_revision = SHELL_DEFAULT_ORG_REVISION

    org: str = field(
        required=True,
        description=(
            "Slug of the organization the dashboard shell renders by "
            "default on this node. Rendering default only — never an "
            "authority or request-scoping input."
        ),
    )


def shell_default_org() -> str:
    """Resolve this node's declared shell default org.

    # org-scope: machine — the declaration above; first-run seeds it. A
    # node initialized before the seed existed falls back to its first
    # listed shared organization. A UI/attribution default only — never a
    # request-scoping input.
    """
    from tools.graph import settings_ops

    try:
        members = settings_ops.read_owned_set(
            SHELL_DEFAULT_ORG_SET_ID, org="machine",
        ).members
        for m in members:
            if m.key == SHELL_DEFAULT_ORG_KEY and m.payload.get("org"):
                return m.payload["org"]
    except Exception:
        pass
    from tools.graph import org_ops

    orgs = [r.slug for r in org_ops.list_orgs() if r.type == "shared"]
    return orgs[0] if orgs else "autonomy"
