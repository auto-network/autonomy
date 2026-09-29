"""Inbox application labels and the page an approval opens on."""

from __future__ import annotations

from urllib.parse import urlencode


#: application_scope -> (label, icon_ref), shown by the inbox and Web Push.
APPLICATIONS = {
    "worktrees": ("Worktrees", "attention.application.worktrees"),
    "jira": ("Jira", "attention.application.jira"),
    "mailbox": ("Mail", "attention.application.mailbox"),
    "links": ("Links", "attention.application.links"),
    "sessions": ("Sessions", "attention.application.sessions"),
    "mission_control": ("Mission Control", "attention.application.mission_control"),
    "vault": ("Vault", "attention.application.vault"),
    "relay": ("Relay", "attention.application.relay"),
    "fleet": ("Fleet", "attention.application.fleet"),
    "dropbox": ("Dropbox", "attention.application.dropbox"),
}


def destination_route(
    route_builder_id: str, destination_id: str, source_guard_ref: str,
) -> str:
    """The dashboard path an approval opens on, for a Web Push payload."""
    if (route_builder_id, destination_id) == ("activity.approval.v1", "activity.approval"):
        return "/activity?" + urlencode({"focus": "approval", "id": source_guard_ref})
    raise ValueError("unregistered attention route builder")


__all__ = ["APPLICATIONS", "destination_route"]
