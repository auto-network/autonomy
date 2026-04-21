"""GraphClient — single dispatch point for graph reads and writes.

CLI commands call ``get_client().method(...)``. The "am I in a container?"
branch lives here in exactly one place: if ``GRAPH_API`` is set we route
HTTP through the dashboard (single-writer + WAL-fresh reads); otherwise we
call ``ops.*`` directly against the local DB.

**Adding a new cmd_**: always go through ``get_client()``, never through
``ops.*`` directly — that's what the client dispatch is for. The
``test_cli_client_conformance.py`` AST test enforces this; a new
``_ops.X(...)`` call inside a ``cmd_*`` body fails CI.

The HttpClient mirrors the LocalClient interface so call sites are
identical in either mode. Cross-org write mismatches come back from the
dashboard as HTTP 409 and are translated to
``ops.CrossOrgWriteError`` so ``except`` blocks stay unchanged.

Design reference: graph://bcce359d-a1d (Cross-Org Search Architecture).
"""

from __future__ import annotations

import json as _json
import mimetypes
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import ops


class GraphHttpError(Exception):
    """Raised when the dashboard graph API returns a non-translatable error."""

    def __init__(self, message: str, status: int, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


def _translate_http_error(status: int, body: dict) -> Exception:
    """Convert a dashboard API error response into the exception the
    local-mode callers already handle (so cmd_ bodies stay unchanged).

    The dashboard's 409 body carries ``origin_org`` and ``target_id`` (see
    ``_cross_org_error_response`` in dashboard/server.py) — that pair is
    the signature of a CrossOrgWriteError regardless of the human-readable
    ``error`` message.
    """
    if status == 409 and body.get("origin_org") is not None:
        target = body.get("target_id") or body.get("source_id") or ""
        origin = body.get("origin_org") or ""
        return ops.CrossOrgWriteError(target, origin)
    if status == 404:
        msg = body.get("error") or "not found"
        return LookupError(msg)
    if status == 400:
        msg = body.get("error") or "bad request"
        return ValueError(msg)
    return GraphHttpError(body.get("error") or f"HTTP {status}", status, body)



class HttpClient:
    """Routes reads and writes through the dashboard API.

    Used in containers where ``GRAPH_API`` is set. Single-writer via the
    host dashboard means the container never needs to open graph.db files
    (and the bind mount can stay read-only).

    Cross-org write mismatches from the server (HTTP 409) are translated
    back into :class:`ops.CrossOrgWriteError` so ``cmd_`` ``except`` blocks
    stay unchanged.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── transport ──────────────────────────────────────────

    def _headers(self, org: str | None = None) -> dict:
        """Build per-request headers.

        Precedence for ``X-Graph-Org``: explicit ``org`` arg > ``GRAPH_ORG``
        env. Matches how ``ops.*`` resolves the caller org on the host so
        the CLI reaches the same DB in both modes without callers having
        to pass ``--org`` explicitly.
        """
        h = {}
        caller = org or os.environ.get("GRAPH_ORG")
        if caller:
            h["X-Graph-Org"] = caller
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        headers: dict | None = None,
        raw_data: bytes | None = None,
        content_type: str | None = None,
        timeout: int = 30,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None},
                doseq=True,
            )
            url = f"{url}?{qs}"
        if raw_data is not None:
            data = raw_data
        elif body is not None:
            data = _json.dumps(body).encode()
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
        else:
            data = None
        if content_type:
            headers = dict(headers or {})
            headers["Content-Type"] = content_type
        req = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method,
        )
        try:
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx,
            )
            raw = resp.read()
            if not raw:
                return None
            return _json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                err_body = _json.loads(e.read())
            except (_json.JSONDecodeError, Exception):
                err_body = {"error": str(e)}
            raise _translate_http_error(e.code, err_body) from None
        except urllib.error.URLError as e:
            raise GraphHttpError(
                f"Cannot reach graph API at {self.base_url}: {e.reason}", 0,
            ) from None

    def _get(self, path, params=None, *, org=None):
        return self._request("GET", path, params=params, headers=self._headers(org))

    def _post(self, path, body, *, org=None):
        return self._request("POST", path, body=body, headers=self._headers(org))

    def _put(self, path, body=None, *, org=None):
        return self._request(
            "PUT", path, body=body or {}, headers=self._headers(org),
        )

    def _delete(self, path, *, org=None):
        return self._request("DELETE", path, headers=self._headers(org))

    # ── reads ──────────────────────────────────────────────

    def search(
        self, q, *, org=None, peers=None, only_org=None, limit=25,
        project=None, or_mode=False, tag=None, states=None,
        include_raw=False, session_source_ids=None,
        session_author_pattern=None,
    ):
        params: dict[str, Any] = {"q": q, "limit": str(limit)}
        if project:
            params["project"] = project
        if or_mode:
            params["or"] = "1"
        if tag:
            params["tag"] = tag
        if states:
            params["states"] = ",".join(states)
        if include_raw:
            params["include_raw"] = "1"
        if only_org:
            params["only_org"] = only_org
        if peers is not None:
            params["peers"] = ",".join(peers)
        if session_source_ids:
            params["session_source_ids"] = ",".join(session_source_ids)
        if session_author_pattern:
            params["session_author_pattern"] = session_author_pattern
        result = self._get("/api/graph/search", params, org=org)
        return result if isinstance(result, list) else []

    def get_source(self, source_id, *, org=None, peers=None):
        try:
            return self._get(f"/api/graph/source/{source_id}", org=org)
        except LookupError:
            return None

    def get_attachment(self, attachment_id, *, org=None, peers=None):
        try:
            return self._get(f"/api/graph/attachment/{attachment_id}", org=org)
        except LookupError:
            return None

    def list_attachments(self, source_id=None, *, org=None, peers=None, limit=50):
        if not source_id:
            raise NotImplementedError(
                "HttpClient.list_attachments requires source_id"
            )
        result = self._get(f"/api/source/{source_id}/attachments", org=org)
        if isinstance(result, dict) and "attachments" in result:
            return result["attachments"]
        return []

    def list_sources(
        self, *, org=None, peers=None, only_org=None, limit=50, project=None,
        source_type=None, tags=None, since=None, until=None, author=None,
        states=None, include_raw=False,
        session_source_ids=None, session_author_pattern=None,
    ):
        params: dict[str, Any] = {"limit": str(limit)}
        if project:
            params["project"] = project
        if source_type:
            params["type"] = source_type
        if tags:
            params["tags"] = ",".join(tags)
        if only_org:
            params["only_org"] = only_org
        if peers is not None:
            params["peers"] = ",".join(peers)
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if author:
            params["author"] = author
        if states:
            params["states"] = ",".join(states)
        if include_raw:
            params["include_raw"] = "1"
        if session_source_ids:
            params["session_source_ids"] = ",".join(session_source_ids)
        if session_author_pattern:
            params["session_author_pattern"] = session_author_pattern
        result = self._get("/api/graph/sources", params, org=org)
        if isinstance(result, dict) and "sources" in result:
            return result["sources"]
        return result if isinstance(result, list) else []

    def list_attention(
        self, *, org=None, since=None, search=None, last=None, session=None,
        context=0,
    ):
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        if search:
            params["search"] = search
        if last is not None:
            params["last"] = str(last)
        if session:
            params["session"] = session
        if context:
            params["context"] = str(context)
        result = self._get("/api/graph/attention", params or None, org=org)
        if isinstance(result, dict) and "rows" in result:
            return result["rows"]
        return result if isinstance(result, list) else []

    def list_collab_topics(self, *, org=None):
        result = self._get("/api/graph/collab-topics", org=org)
        if isinstance(result, dict) and "topics" in result:
            return result["topics"]
        return result if isinstance(result, list) else []

    def list_collab_sources(self, *, org=None, limit=50):
        result = self._get("/api/graph/collab", {"limit": str(limit)}, org=org)
        if isinstance(result, dict) and "notes" in result:
            return result["notes"]
        return result if isinstance(result, list) else []

    def resolve_source_strict(self, source_id, *, org=None, peers=None):
        # Server's GET /api/graph/source/{id} already does own-first +
        # peer-public-surface resolve. The dashboard never returns
        # ambiguous prefix lists over HTTP (callers pass full UUIDs), so
        # dict-or-None is the only shape we need to map.
        return self.get_source(source_id, org=org, peers=peers)

    def get_turn_content(self, source_id, turn, *, org=None):
        try:
            result = self._get(
                f"/api/graph/turn/{source_id}",
                {"turn": str(turn)},
                org=org,
            )
        except LookupError:
            return None
        if isinstance(result, dict):
            return result.get("content")
        return None

    def get_comment(self, comment_id, *, org=None):
        try:
            return self._get(f"/api/graph/comment/{comment_id}", org=org)
        except LookupError:
            return None

    # ── writes ─────────────────────────────────────────────

    def create_note(
        self, content, *, tags=None, author=None, project=None,
        attachments=None, html_path=None,
        auto_provenance_source_id=None, auto_provenance_turn=None,
        org=None,
    ):
        if attachments or html_path:
            return self._create_note_multipart(
                content,
                tags=tags, author=author, project=project,
                attachments=attachments, html_path=html_path,
                auto_provenance_source_id=auto_provenance_source_id,
                auto_provenance_turn=auto_provenance_turn,
                org=org,
            )
        body: dict[str, Any] = {"content": content}
        if tags:
            body["tags"] = ",".join(tags)
        if author:
            body["author"] = author
        if project:
            body["project"] = project
        if auto_provenance_source_id:
            body["auto_provenance_source_id"] = auto_provenance_source_id
        if auto_provenance_turn:
            body["auto_provenance_turn"] = auto_provenance_turn
        result = self._post("/api/graph/note", body, org=org)
        return _normalize_note_result(result, content)

    def _create_note_multipart(
        self, content, *, tags, author, project,
        attachments, html_path,
        auto_provenance_source_id, auto_provenance_turn, org,
    ):
        fields: dict[str, str] = {"content": content}
        if tags:
            fields["tags"] = ",".join(tags)
        if author:
            fields["author"] = author
        if project:
            fields["project"] = project
        if auto_provenance_source_id:
            fields["auto_provenance_source_id"] = auto_provenance_source_id
        if auto_provenance_turn is not None:
            fields["auto_provenance_turn"] = str(auto_provenance_turn)
        files: list[tuple[str, str, bytes, str]] = []
        if html_path:
            files.append(_file_tuple("html", html_path))
        for fp in attachments or []:
            files.append(_file_tuple("attachments", fp))
        body, ctype = _build_multipart(fields, files)
        result = self._request(
            "POST", "/api/graph/note",
            raw_data=body, content_type=ctype,
            headers=self._headers(org), timeout=60,
        )
        return _normalize_note_result(result, content)

    def update_note(
        self, source_id, content, *, integrate_comments=None,
        attachments=None, html_path=None, org=None,
    ):
        if attachments or html_path:
            return self._update_note_multipart(
                source_id, content,
                integrate_comments=integrate_comments,
                attachments=attachments, html_path=html_path, org=org,
            )
        body: dict[str, Any] = {
            "source_id": source_id,
            "content": content,
        }
        if integrate_comments:
            body["integrate_ids"] = list(integrate_comments)
        result = self._post("/api/graph/note/update", body, org=org)
        return _normalize_update_result(result, content)

    def _update_note_multipart(
        self, source_id, content, *, integrate_comments, attachments,
        html_path, org,
    ):
        fields: dict[str, str] = {
            "source_id": source_id,
            "content": content,
        }
        if integrate_comments:
            fields["integrate_ids"] = _json.dumps(list(integrate_comments))
        files: list[tuple[str, str, bytes, str]] = []
        if html_path:
            files.append(_file_tuple("html", html_path))
        for fp in attachments or []:
            files.append(_file_tuple("attachments", fp))
        body, ctype = _build_multipart(fields, files)
        result = self._request(
            "POST", "/api/graph/note/update",
            raw_data=body, content_type=ctype,
            headers=self._headers(org), timeout=60,
        )
        return _normalize_update_result(result, content)

    def add_comment(self, source_id, content, *, actor="user", org=None):
        body = {"source_id": source_id, "content": content, "actor": actor}
        result = self._post("/api/graph/comment", body, org=org)
        return {
            "id": result.get("comment_id"),
            "source_id": result.get("source_id"),
        }

    def integrate_comment(self, comment_id, *, org=None):
        result = self._post(
            "/api/graph/comment/integrate",
            {"comment_id": comment_id},
            org=org,
        )
        if isinstance(result, dict):
            return bool(result.get("changed", True))
        return True

    def create_edge(
        self, from_id, to_id, *, from_type="source", to_type="source",
        relation="informed_by", turns=None, note=None, org=None,
    ):
        body: dict[str, Any] = {
            "bead_id": from_id,
            "source_id": to_id,
            "relationship": relation,
        }
        if from_type != "bead":
            body["from_type"] = from_type
        if to_type != "source":
            body["to_type"] = to_type
        if turns is not None:
            if isinstance(turns, (tuple, list)) and len(turns) == 2:
                body["turn"] = f"{turns[0]}-{turns[1]}"
            else:
                body["turn"] = str(turns)
        if note:
            body["note"] = note
        result = self._post("/api/graph/link", body, org=org)
        return {
            "id": result.get("edge_id"),
            "source_id": result.get("bead_id"),
            "target_id": result.get("source_id"),
            "relation": result.get("relation") or relation,
        }

    def attach_file(
        self, file_path, *, source_id=None, turn_number=None,
        alt_text=None, original_filename=None, org=None,
    ):
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(str(file_path))
        fields: dict[str, str] = {}
        if source_id:
            fields["source_id"] = source_id
        if turn_number is not None:
            fields["turn"] = str(turn_number)
        if alt_text:
            fields["alt_text"] = alt_text
        files = [_file_tuple(
            "file", str(file_path),
            filename=original_filename or path.name,
        )]
        body, ctype = _build_multipart(fields, files)
        result = self._request(
            "POST", "/api/graph/attach",
            raw_data=body, content_type=ctype,
            headers=self._headers(org), timeout=60,
        )
        return {
            "id": result.get("attachment_id"),
            "filename": result.get("filename"),
            "size_bytes": result.get("size_bytes"),
            "source_id": result.get("source_id"),
            "mime_type": result.get("mime_type"),
        }

    # ── settings ───────────────────────────────────────────

    def list_set_ids(self, *, org=None):
        result = self._get("/api/graph/sets", org=org)
        if isinstance(result, dict) and "set_ids" in result:
            return result["set_ids"]
        return result if isinstance(result, list) else []

    def read_set(self, set_id, *, target_revision=None, min_revision=None, org=None):
        from .settings_ops import SetMembers, ResolvedSetting, DropAccounting
        params: dict[str, Any] = {}
        if target_revision is not None:
            params["as_rev"] = str(target_revision)
        if min_revision is not None:
            params["min_rev"] = str(min_revision)
        result = self._get(f"/api/graph/settings/{set_id}", params, org=org)
        return SetMembers(
            members=[_dict_to_resolved_setting(m) for m in result.get("members", [])],
            dropped=DropAccounting(**(result.get("dropped") or {})),
        )

    def get_setting(self, setting_id, *, target_revision=None, org=None):
        params: dict[str, Any] = {}
        if target_revision is not None:
            params["as_rev"] = str(target_revision)
        try:
            result = self._get(
                f"/api/graph/setting/{setting_id}", params, org=org,
            )
        except LookupError:
            return None
        return _dict_to_resolved_setting(result)

    def add_setting(
        self, set_id, schema_revision, key, payload, *, state="raw", org=None,
    ):
        body = {
            "set_id": set_id,
            "schema_revision": schema_revision,
            "key": key,
            "payload": payload,
            "state": state,
        }
        result = self._post("/api/graph/setting", body, org=org)
        return result.get("id")

    def override_setting(self, target_id, payload, *, state="raw", org=None):
        body = {"payload": payload, "state": state}
        result = self._post(
            f"/api/graph/setting/{target_id}/override", body, org=org,
        )
        return result.get("id")

    def exclude_setting(self, target_id, *, state="raw", org=None):
        body = {"state": state}
        result = self._post(
            f"/api/graph/setting/{target_id}/exclude", body, org=org,
        )
        return result.get("id")

    def promote_setting(self, setting_id, to_state, *, org=None):
        self._post(
            f"/api/graph/setting/{setting_id}/promote",
            {"to_state": to_state},
            org=org,
        )

    def deprecate_setting(self, setting_id, *, successor_id=None, org=None):
        body: dict[str, Any] = {}
        if successor_id:
            body["successor_id"] = successor_id
        self._post(
            f"/api/graph/setting/{setting_id}/deprecate", body, org=org,
        )

    def remove_setting(self, setting_id, *, org=None):
        self._delete(f"/api/graph/setting/{setting_id}", org=org)

    def migrate_setting_revisions(
        self, set_id, to_rev, *, dry_run=False, org=None,
    ):
        from .settings_ops import MigrationReport
        body = {"to_rev": to_rev, "dry_run": dry_run}
        result = self._post(
            f"/api/graph/settings/{set_id}/migrate", body, org=org,
        )
        return MigrationReport(
            set_id=result.get("set_id", set_id),
            to_revision=result.get("to_revision", to_rev),
            dry_run=result.get("dry_run", dry_run),
            rewrote=result.get("rewrote", 0),
            no_upconvert_path=result.get("no_upconvert_path", 0),
            already_at_target=result.get("already_at_target", 0),
            above_target=result.get("above_target", 0),
            affected_ids=result.get("affected_ids") or [],
        )

    # ── tags ───────────────────────────────────────────────

    def add_tag(self, source_id, tag, *, org=None):
        """PUT /api/graph/tag/{source_id}/{tag}. Returns True if newly added."""
        result = self._put(f"/api/graph/tag/{source_id}/{tag}", org=org)
        return bool(result.get("added"))

    def remove_tag(self, source_id, tag, *, org=None):
        """DELETE /api/graph/tag/{source_id}/{tag}. Returns True if removed."""
        result = self._delete(f"/api/graph/tag/{source_id}/{tag}", org=org)
        return bool(result.get("removed"))

    def tag_merge(self, from_tag, to_tag, *, reason="", force=False, org=None):
        body = {"from": from_tag, "to": to_tag, "reason": reason, "force": force}
        return self._post("/api/graph/tag/merge", body, org=org) or {}

    def update_tag_description(self, tag_name, description, *, actor="user", org=None):
        body = {"description": description, "actor": actor}
        return self._put(
            f"/api/graph/collab/tag-describe/{tag_name}", body, org=org,
        ) or {}

    def set_collab_tag(self, source_id, *, org=None):
        return self._put(f"/api/graph/collab/tag/{source_id}", {}, org=org) or {}

    # ── thoughts / threads ─────────────────────────────────

    def insert_capture(
        self, capture_id, content, *,
        source_id=None, turn_number=None, thread_id=None,
        actor="user", org=None,
    ):
        body = {
            "capture_id": capture_id,
            "content": content,
            "actor": actor,
        }
        if source_id:
            body["source_id"] = source_id
        if turn_number is not None:
            body["turn_number"] = turn_number
        if thread_id:
            body["thread_id"] = thread_id
        return self._post("/api/graph/thought", body, org=org) or {}

    def list_captures(self, *, thread_id=None, since=None, limit=50, org=None):
        params = {"limit": str(limit)}
        if thread_id:
            params["thread"] = thread_id
        if since:
            params["since"] = since
        result = self._get("/api/graph/thoughts", params, org=org)
        # Server returns ``{"thoughts": [...]}`` — the endpoint name is the
        # user-facing "thoughts" but the rows are capture records.
        if isinstance(result, dict):
            for k in ("thoughts", "captures"):
                if k in result:
                    return result[k]
        return result if isinstance(result, list) else []

    def insert_thread(
        self, thread_id, title, *, priority=1, created_by="user", org=None,
    ):
        body = {
            "thread_id": thread_id, "title": title,
            "priority": priority, "created_by": created_by,
        }
        return self._post("/api/graph/thread", body, org=org) or {}

    def list_threads(self, *, status=None, include_all=False, limit=50, org=None):
        params = {"limit": str(limit)}
        if include_all:
            params["all"] = "1"
        elif status:
            params["status"] = status
        result = self._get("/api/graph/threads", params, org=org)
        if isinstance(result, dict) and "threads" in result:
            return result["threads"]
        return result if isinstance(result, list) else []

    def thread_action(self, action, thread_id, *, target=None, org=None):
        body = {"action": action, "thread_id": thread_id}
        if target:
            body["target"] = target
        return self._post("/api/graph/thread/action", body, org=org) or {}

    def get_thread(self, thread_id, *, org=None):
        try:
            return self._get(f"/api/graph/thread/{thread_id}", org=org)
        except LookupError:
            return None

    # ── bead / journal / sessions ──────────────────────────

    def create_bead(
        self, title, *, priority=2, description=None, bead_type=None,
        source=None, turns=None, note=None, org=None,
    ):
        body = {"title": title, "priority": priority}
        if description:
            body["description"] = description
        if bead_type:
            body["type"] = bead_type
        if source:
            body["source"] = source
        if turns:
            body["turns"] = turns
        if note:
            body["note"] = note
        return self._post("/api/graph/bead", body, org=org) or {}

    def write_journal_entry(self, payload, *, org=None):
        return self._post("/api/graph/journal", payload, org=org) or {}

    def ingest_sessions(self, *, all_projects=False, project=None, force=False, org=None):
        body = {}
        if all_projects:
            body["all"] = True
        if project:
            body["project"] = project
        if force:
            body["force"] = True
        return self._post("/api/graph/sessions", body, org=org) or {}

    # ── stats / tree / entities ────────────────────────────

    def stats(self, *, org=None):
        return self._get("/api/graph/stats", org=org) or {}

    def get_tree(self, root=None, *, depth=3, org=None):
        params: dict[str, Any] = {"depth": str(depth)}
        if root:
            params["root"] = root
        result = self._get("/api/graph/tree", params, org=org)
        if isinstance(result, dict) and "nodes" in result:
            return result["nodes"]
        return result if isinstance(result, list) else []

    def list_entities(self, *, entity_type=None, limit=20, org=None):
        params: dict[str, Any] = {"limit": str(limit)}
        if entity_type:
            params["type"] = entity_type
        result = self._get("/api/graph/entities", params, org=org)
        if isinstance(result, dict) and "entities" in result:
            return result["entities"]
        return result if isinstance(result, list) else []

    def search_entities(self, query, *, limit=20, org=None):
        params: dict[str, Any] = {"query": query, "limit": str(limit)}
        result = self._get("/api/graph/entities", params, org=org)
        if isinstance(result, dict) and "entities" in result:
            return result["entities"]
        return result if isinstance(result, list) else []

    def entity_thoughts(self, entity_id, *, limit=20, org=None):
        params = {"limit": str(limit)}
        result = self._get(f"/api/graph/entity/{entity_id}/thoughts", params, org=org)
        if isinstance(result, dict) and "thoughts" in result:
            return result["thoughts"]
        return result if isinstance(result, list) else []

    def entity_mention_count(self, entity_id, *, org=None):
        """On host callers short-circuit; container uses annotated entities."""
        raise NotImplementedError(
            "container callers should read the 'mentions' field embedded in "
            "list_entities / search_entities output; this helper stays host-only.",
        )


# ── helpers ─────────────────────────────────────────────────────


def _file_tuple(field_name: str, file_path: str, *, filename: str | None = None) -> tuple[str, str, bytes, str]:
    p = Path(file_path)
    fname = filename or p.name
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(fname)
    return (field_name, fname, data, mime or "application/octet-stream")


def _build_multipart(fields: dict, files: list) -> tuple[bytes, str]:
    """Assemble a multipart/form-data body.

    ``fields``: ``{key: str}``
    ``files``:  ``[(field_name, filename, bytes, content_type), ...]``
    """
    boundary = "----GraphClientMultipartBoundary"
    body = b""
    for k, v in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        ).encode()
    for field_name, filename, data, ctype in files:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _normalize_note_result(result: dict, content: str) -> dict:
    """Map server-side note-create response into the ``ops.create_note`` shape."""
    if not isinstance(result, dict):
        result = {}
    lines = content.count("\n") + (1 if content else 0)
    return {
        "id": result.get("source_id"),
        "source_id": result.get("source_id"),
        "title": result.get("title") or content[:80],
        "org": result.get("org") or "",
        "lines": result.get("lines", lines),
        "chars": result.get("chars", len(content)),
        "content": content,
        "attachments": result.get("attachments") or [],
        "rich_content": bool(result.get("rich_content")),
        "auto_provenance": result.get("auto_provenance"),
    }


def _normalize_update_result(result: dict, content: str) -> dict:
    """Map server-side note-update response into the ``ops.update_note`` shape."""
    if not isinstance(result, dict):
        result = {}
    lines = content.count("\n") + (1 if content else 0)
    return {
        "source_id": result.get("source_id"),
        "new_version": result.get("new_version"),
        "org": result.get("org") or "",
        "lines": result.get("lines", lines),
        "chars": result.get("chars", len(content)),
        "content": content,
        "integrated": result.get("integrated") or [],
        "not_found_comments": result.get("not_found_comments") or [],
        "attachments": result.get("attachments") or [],
        "rich_content": bool(result.get("rich_content")),
    }


def _dict_to_resolved_setting(d: dict):
    """Reconstruct a ``ResolvedSetting`` from the dashboard API response."""
    from .settings_ops import ResolvedSetting
    return ResolvedSetting(
        id=d["id"],
        set_id=d["set_id"],
        stored_revision=d["stored_revision"],
        key=d["key"],
        payload=d.get("payload"),
        state=d.get("state", "raw"),
        supersedes=d.get("supersedes"),
        excludes=d.get("excludes"),
        deprecated=bool(d.get("deprecated", False)),
        successor_id=d.get("successor_id"),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        target_revision=d.get("target_revision"),
        org=d.get("org"),
        upconverted=bool(d.get("upconverted", False)),
    )


# ── Dispatcher ──────────────────────────────────────────────────


def get_client():
    """Return the right graph client for the current environment.

    Container (GRAPH_API set) → :class:`HttpClient` over HTTPS.
    Host                          → the :mod:`ops` module itself; it
                                    duck-types as a client since every
                                    method HttpClient exposes is already
                                    a top-level `ops.X` function with
                                    the same name and signature.
    """
    api = os.environ.get("GRAPH_API")
    if api:
        return HttpClient(api)
    from . import ops
    return ops
