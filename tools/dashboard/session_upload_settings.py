"""Setting schema for dashboard-uploaded files in a session.

Bead from this conversation. The session viewer's existing
``viewer_attachment`` tile renderer is the right surface for showing
inline thumbnails of operator-uploaded screenshots — the work is to
get a properly-shaped entry in front of the renderer at the right
timestamp position, without reverse-engineering it from user-turn
text or polling a directory.

The substrate is the right home: at upload time, ``api_upload``
writes one row per upload to this set; the session viewer subscribes
via ``Schema.of('dashboard.session.upload')`` and merges each row in
as a synthesised ``viewer_attachment`` entry, sorted into the
displayed timeline by the row's ``timestamp`` field.

Per-session scoping is by payload (``target_session``), not by
set-id namespace. Append-only-log so each upload is a discrete
event with a UUID key — no collision concerns even if the same
filename gets uploaded twice in the same session.
"""
from tools.graph.schemas.registry import (
    publication_band,
    home,
    SettingSchema,
    append_only_log,
    field,
)


SESSION_UPLOAD_SET_ID = "dashboard.session.upload"
SCHEMA_REVISION = 1


#: THIS MACHINE ALONE, because the FILE is on this machine alone.
#:
#: This was ``@home("organization")``, and the reasoning recorded here was
#: about OWNERSHIP -- "the operator owns workspaces, so their database is the
#: organizational home of their own things". That is a fair principle and the
#: wrong axis for this decision. The question is not who owns the row, it is
#: where the BYTES are: ``api_upload`` writes the file to local disk on the
#: machine that received it, and it cannot be produced anywhere else.
#:
#: Replicating the row without the bytes did not make the upload available
#: elsewhere -- it made every other machine render a viewer_attachment TILE FOR
#: A FILE THAT IS NOT THERE, because the session viewer merges each row in as a
#: tile at its timestamp. 106 such rows were replicated to sjc-2 (auto-wilkh).
#: Machine-homing the row makes it follow the file, converting a falsely
#: present attachment into an honestly absent one, with no transport work.
#:
#: The general rule this violated: a replicated row must never point at a
#: machine-local file. It either carries the content, or it lives with the
#: file. See graph://f331232d-fd4 -- this is one of three instances found in
#: one night, alongside the serve-cert outage (auto-527te).
@publication_band(min="raw", max="curated")
@home("machine")
@append_only_log
class SessionUploadV1(SettingSchema):
    """One row per dashboard-uploaded file."""

    set_id = SESSION_UPLOAD_SET_ID
    schema_revision = SCHEMA_REVISION

    target_session: str = field(
        default="",
        description="Tmux session the upload was paste-bound to.",
    )
    filename: str = field(
        default="",
        description="Final on-disk filename after sanitisation + dedup-counter.",
    )
    rel_path: str = field(
        default="",
        description=(
            "Path under the session run dir, relative — e.g. "
            "'.uploads/foo.png'. Resolved via the existing "
            "/api/session/{tmux}/output/{path:path} serve route, so "
            "viewer_attachment.viewerAttachmentSrc renders it without "
            "needing a second serve route."
        ),
    )
    mime: str = field(
        default="",
        description="MIME type guessed from extension at upload time.",
    )
    size: int = field(
        default=0,
        description="File size in bytes.",
    )
    timestamp: str = field(
        default="",
        description=(
            "ISO 8601 UTC, captured at upload time. Drives the "
            "viewer's timestamp-merge into the displayed entries."
        ),
    )
