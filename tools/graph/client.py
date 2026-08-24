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
import time
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


class NotFoundError(LookupError):
    """404 from the dashboard graph API, carrying the response body.

    Subclasses :class:`LookupError` so every existing ``except LookupError``
    handler keeps working; ``body`` preserves server-side hints (e.g.
    ``exists_in_org`` on a cross-org source miss) for callers that can
    render a better error than a bare not-found.
    """

    def __init__(self, message: str, body: dict | None = None):
        super().__init__(message)
        self.body = body or {}


def _resolve_client_org_arg(org):
    """Mirror :func:`settings_ops._resolve_org_arg` for HTTP-client use.

    Settings methods (auto-cfb8u) require ``org=``. The
    :data:`settings_ops.CALLER_ORG` sentinel resolves to ``None`` on the
    client side — the client asserts no scope of its own; the server
    derives a container's org from its session token.

    A non-empty string slug routes that org. ``None`` is preserved as
    "no ``X-Graph-Org`` header" — the server treats that as scopeless.
    """
    from .settings_ops import _CallerOrgSentinel
    if isinstance(org, _CallerOrgSentinel):
        return None
    return org


def _settings_headers(org: str | None) -> dict:
    """Build the ``X-Graph-Org`` header dict for a Settings call.

    ``org`` is the post-:func:`_resolve_client_org_arg` value: a slug
    (sets the header) or ``None`` (omits it, signalling scopeless to the
    server). No env fallback — Settings methods own their cascade.
    """
    return {"X-Graph-Org": org} if org else {}


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
        return NotFoundError(msg, body)
    if status == 400:
        msg = body.get("error") or "bad request"
        # Schema validation 400s carry the specific failure in ``detail``
        # (see ``server.py`` schema-validation responses). Dropping it
        # leaves the caller with a bare "schema validation failed" and no
        # indication of which field is wrong or why.
        detail = body.get("detail")
        if detail and detail != msg:
            msg = f"{msg}: {detail}"
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
        #: Full body of the most recent Settings write. The server reports
        #: a stored row that resolution will never return; that report is
        #: only useful if it survives the transport, and returning the id
        #: alone discarded it. Held here so the caller can say so.
        self.last_write_report: dict | None = None
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── transport ──────────────────────────────────────────

    def _headers(self, org: str | None = None) -> dict:
        """Build per-request headers.

        ``X-Graph-Org`` is sent only for an explicit ``org`` argument —
        a deliberate per-call selection. The client reads no ambient
        scope: a container's org is stamped on its session token and
        enforced server-side; a host caller names an org or omits one.

        A container session token (``CROSSTALK_TOKEN``) is sent additively as
        ``Authorization: Bearer`` so the server can take a remote caller's org
        from the authenticated token rather than the caller-controlled header
        (auto-w1ktf, the client half of invariant 1). This is ADDITIVE: the
        bearer rides ALONGSIDE ``X-Graph-Org`` and changes no server behavior
        until the ``auto-h4kzx`` server flip derives org from the token and
        ignores the header — so there is no window where a token-requiring
        server refuses a caller that has not yet sent one. A host caller has no
        ``CROSSTALK_TOKEN`` and simply omits the bearer (a local, org-less
        caller server-side).
        """
        h = {}
        caller = org or None
        if caller:
            h["X-Graph-Org"] = caller
        token = os.environ.get("CROSSTALK_TOKEN")
        if token:
            h["Authorization"] = f"Bearer {token}"
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
        return_bytes: bool = False,
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
        # The session bearer is attached HERE, at the one chokepoint every
        # request passes through, rather than in a header builder.
        #
        # There are two builders — `_headers` and the module-level
        # `_settings_headers` — and only the first ever carried the bearer.
        # Every Settings call uses the second, so the CLI presented no
        # credential on exactly the path this was meant to authenticate,
        # while a test asserting on `_headers` stayed green. Attaching it per
        # builder is what allowed one to be missed; attaching it per REQUEST
        # means a third builder cannot reintroduce the gap.
        #
        # A host caller has no CROSSTALK_TOKEN and simply sends none, which
        # is correct: host callers do not use this transport at all (no
        # GRAPH_API), and a local org-less caller is classified on its own
        # terms server-side.
        token = os.environ.get("CROSSTALK_TOKEN")
        if token:
            headers = dict(headers or {})
            headers.setdefault("Authorization", f"Bearer {token}")
        req = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method,
        )
        try:
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx,
            )
            raw = resp.read()
            if return_bytes:
                return raw
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

    def _get_bytes(self, path, *, org=None):
        return self._request(
            "GET", path, headers=self._headers(org), return_bytes=True,
        )

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
        or_mode=False, tag=None, states=None,
        include_raw=False, session_source_ids=None,
        session_author_pattern=None, source_type=None, ranker="legacy",
    ):
        params: dict[str, Any] = {"q": q, "limit": str(limit)}
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
        if source_type:
            params["source_type"] = ",".join(source_type)
        if ranker != "legacy":
            params["ranker"] = ranker
        result = self._get("/api/graph/search", params, org=org)
        return result if isinstance(result, list) else []

    def get_source(self, source_id, *, org=None, peers=None):
        try:
            return self._get(f"/api/graph/source/{source_id}", org=org)
        except LookupError:
            return None

    def locate_source_org(self, source_id, *, org=None):
        """Existence probe for not-found errors, mirror of
        :func:`ops.locate_source_org`.

        Returns ``{"org": slug, "id": full_id, "type": ...}`` when the ID
        resolves — either inside the caller's scope, or (via the server's
        enriched 404 body) in an org the caller can't see. ``None`` when
        the ID exists nowhere.
        """
        try:
            src = self._get(f"/api/graph/source/{source_id}", org=org)
        except NotFoundError as e:
            if e.body.get("exists_in_org"):
                return {
                    "org": e.body["exists_in_org"],
                    "id": e.body.get("source_id") or source_id,
                    "type": e.body.get("source_type") or "",
                }
            return None
        except LookupError:
            return None
        if not isinstance(src, dict):
            return None
        return {
            "org": src.get("org") or "",
            "id": src.get("id") or source_id,
            "type": src.get("type") or "",
        }

    def read_source_full(
        self,
        source_id,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
        around_turn: int | None = None,
        window: int = 5,
        tail_n: int | None = None,
    ):
        """Mirror of :func:`ops.read_source_full` over HTTP.

        Routes through ``/api/graph/{id}``. ``tail_n=N`` is sent as
        ``?from=-N`` so the server resolves ``MAX(turn_number)`` and the
        trailing slice in a single round trip. ``around_turn`` /
        ``window`` map to ``?turn=&window=``. ``peers`` is accepted for
        API parity with the local ``ops`` function but is not forwarded
        — the dashboard handler already does own-first + peer-public
        resolution server-side.

        Returns the **full source — no character cap**. The HTTP route is
        unbounded by design (browser surface). If you intend to feed
        this response into an LLM prompt, cap explicitly at the call
        site or use ``ops.read_source_full(..., max_chars=N)`` directly
        — the wire protocol does not carry a caller-side ``max_chars``.
        """
        params: dict[str, str] = {}
        if around_turn is not None:
            params["turn"] = str(around_turn)
            params["window"] = str(window)
        if tail_n is not None:
            params["from"] = str(-int(tail_n))
        try:
            return self._get(
                f"/api/graph/{source_id}",
                params=params or None,
                org=org,
            )
        except LookupError:
            return None

    def get_attachment(self, attachment_id, *, org=None, peers=None):
        try:
            return self._get(f"/api/graph/attachment/{attachment_id}", org=org)
        except LookupError:
            return None

    def resolve_attachment_strict(self, attachment_id, *, org=None, peers=None):
        result = self._get(
            f"/api/graph/attachment/{attachment_id}",
            {"strict": "1"},
            org=org,
        )
        if not isinstance(result, dict):
            return None
        if result.get("matches") is not None:
            return result["matches"]
        return result.get("attachment")

    def download_attachment(self, attachment_id, *, org=None, peers=None):
        return self._get_bytes(f"/api/attachment/{attachment_id}", org=org)

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
        self, *, org=None, peers=None, only_org=None, limit=50,
        source_type=None, tags=None, since=None, until=None, author=None,
        states=None, include_raw=False,
        session_source_ids=None, session_author_pattern=None,
    ):
        params: dict[str, Any] = {"limit": str(limit)}
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

    def list_session_status(self, *, since=None):
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        result = self._get("/api/dao/session_status", params or None)
        if isinstance(result, dict) and "rows" in result:
            return result["rows"]
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
        self, content, *, tags=None, author=None, session_hint=None,
        attachments=None, html_path=None,
        auto_provenance_source_id=None, auto_provenance_turn=None,
        short_description=None,
        keywords=None,
        org=None,
    ):
        if attachments or html_path:
            return self._create_note_multipart(
                content,
                tags=tags, author=author, session_hint=session_hint,
                attachments=attachments, html_path=html_path,
                auto_provenance_source_id=auto_provenance_source_id,
                auto_provenance_turn=auto_provenance_turn,
                short_description=short_description,
                keywords=keywords,
                org=org,
            )
        body: dict[str, Any] = {"content": content}
        if tags:
            body["tags"] = ",".join(tags)
        if author:
            body["author"] = author
        if session_hint:
            body["session_hint"] = session_hint
        if auto_provenance_source_id:
            body["auto_provenance_source_id"] = auto_provenance_source_id
        if auto_provenance_turn:
            body["auto_provenance_turn"] = auto_provenance_turn
        if short_description:
            body["short_description"] = short_description
        if keywords:
            body["keywords"] = keywords
        result = self._post("/api/graph/note", body, org=org)
        return _normalize_note_result(result, content)

    def _create_note_multipart(
        self, content, *, tags, author, session_hint=None,
        attachments, html_path,
        auto_provenance_source_id, auto_provenance_turn,
        short_description, keywords, org,
    ):
        fields: dict[str, str] = {"content": content}
        if tags:
            fields["tags"] = ",".join(tags)
        if author:
            fields["author"] = author
        if session_hint:
            fields["session_hint"] = session_hint
        if auto_provenance_source_id:
            fields["auto_provenance_source_id"] = auto_provenance_source_id
        if auto_provenance_turn is not None:
            fields["auto_provenance_turn"] = str(auto_provenance_turn)
        if short_description:
            fields["short_description"] = short_description
        if keywords:
            fields["keywords"] = keywords
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
        self, source_id, content=None, *, title=None, integrate_comments=None,
        attachments=None, html_path=None, short_description=None,
        keywords=None, org=None,
    ):
        if attachments or html_path:
            return self._update_note_multipart(
                source_id, content,
                title=title,
                integrate_comments=integrate_comments,
                attachments=attachments, html_path=html_path,
                short_description=short_description,
                keywords=keywords, org=org,
            )
        body: dict[str, Any] = {
            "source_id": source_id,
        }
        if content is not None:
            body["content"] = content
        if title is not None:
            body["title"] = title
        if integrate_comments:
            body["integrate_ids"] = list(integrate_comments)
        if short_description is not None:
            body["short_description"] = short_description
        if keywords is not None:
            body["keywords"] = keywords
        result = self._post("/api/graph/note/update", body, org=org)
        return _normalize_update_result(result, content)

    def _update_note_multipart(
        self, source_id, content, *, title, integrate_comments, attachments,
        html_path, short_description, keywords, org,
    ):
        fields: dict[str, str] = {
            "source_id": source_id,
        }
        if content is not None:
            fields["content"] = content
        if title is not None:
            fields["title"] = title
        if integrate_comments:
            fields["integrate_ids"] = _json.dumps(list(integrate_comments))
        if short_description is not None:
            fields["short_description"] = short_description
        if keywords is not None:
            fields["keywords"] = keywords
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

    def list_note_versions(self, source_id, *, org=None):
        """List every saved version of a note (oldest first).

        Returns ``[{version, content, created_at}, ...]``. Empty list if
        the note has no recorded version history yet (no updates since
        creation). Raises :class:`LookupError` when the source doesn't
        exist or isn't a note.
        """
        result = self._get(f"/api/graph/note/{source_id}/versions", org=org)
        if isinstance(result, dict):
            return result.get("versions") or []
        return []

    def get_note_version(self, source_id, version, *, org=None):
        """Read a specific saved version of a note.

        Returns ``{"version", "content", "created_at", "source_id", "org"}``.
        Raises :class:`LookupError` for missing source or version.
        """
        result = self._get(
            f"/api/graph/note/{source_id}/version/{int(version)}", org=org,
        )
        if not isinstance(result, dict):
            raise LookupError(f"version {version} not found for {source_id}")
        return result

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
    #
    # Mirror of :mod:`tools.graph.settings_ops` — ``org=`` is required on
    # every Settings method (auto-cfb8u). Pass an org slug, the
    # :data:`settings_ops.CALLER_ORG` sentinel for env-cascade
    # semantics, or ``None`` for explicit scopeless. Forgetting ``org=``
    # is a ``TypeError``.

    def list_set_ids(self, *, org):
        org = _resolve_client_org_arg(org)
        result = self._request(
            "GET", "/api/graph/sets", headers=_settings_headers(org),
        )
        if isinstance(result, dict) and "set_ids" in result:
            return result["set_ids"]
        return result if isinstance(result, list) else []

    def read_set(self, set_id, *, org, peers=None, target_revision=None, min_revision=None):
        from .settings_ops import SetMembers, ResolvedSetting, DropAccounting
        org = _resolve_client_org_arg(org)
        params: dict[str, Any] = {}
        if target_revision is not None:
            params["target_revision"] = str(target_revision)
        if min_revision is not None:
            params["min_revision"] = str(min_revision)
        if peers is not None:
            params["peers"] = ",".join(peers)
        result = self._request(
            "GET", f"/api/graph/settings/{set_id}",
            params=params, headers=_settings_headers(org),
        )
        return SetMembers(
            members=[_dict_to_resolved_setting(m) for m in result.get("members", [])],
            dropped=DropAccounting(**(result.get("dropped") or {})),
        )

    def request_vault_open(self, set_id, key, *, org, ttl_seconds=60):
        """Request and await one operator-approved secured Setting release.

        The session identity is intentionally absent from the body: the
        dashboard derives it from this client's bearer.  Held GETs receive
        only a value-free receipt naming the requesting session's ramfs path;
        factor bootstrap is available solely on the browser's operator-cookie
        GET.
        """
        org = _resolve_client_org_arg(org)
        created = self._request(
            "POST",
            "/api/approvals",
            body={
                "kind": "vault_open",
                "request": {
                    "set_id": set_id,
                    "key": key,
                    "ttl_seconds": ttl_seconds,
                },
            },
            headers=_settings_headers(org),
        )
        request_id = (created or {}).get("id")
        if not isinstance(request_id, str) or not request_id:
            raise GraphHttpError("dashboard created no vault-open request", 500)
        deadline = time.monotonic() + ttl_seconds + 5
        result = None
        while result is None and time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            response = self._request(
                "GET",
                f"/api/approvals/{urllib.parse.quote(request_id, safe='')}",
                params={"wait": min(60, int(remaining) or 1)},
                headers=_settings_headers(org),
                timeout=min(65, max(2, int(remaining) + 1)),
            )
            result = (response or {}).get("result")
        if result is None:
            raise GraphHttpError("vault-open approval expired", 408)
        if result.get("approved") is not True:
            raise PermissionError("secured Setting release was declined")
        execution = result.get("execution") or {}
        if execution.get("ok") is not True:
            raise GraphHttpError(
                execution.get("error") or "secured Setting release failed",
                500,
                execution,
            )
        receipt = execution.get("receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("delivery") != "session-ramfs"
            or not isinstance(receipt.get("path"), str)
            or not receipt["path"].startswith("/run/secrets/")
        ):
            raise GraphHttpError("secured Setting returned no ramfs receipt", 500)
        return receipt

    def get_setting(self, setting_id, *, org, target_revision=None):
        org = _resolve_client_org_arg(org)
        params: dict[str, Any] = {}
        if target_revision is not None:
            params["target_revision"] = str(target_revision)
        try:
            result = self._request(
                "GET", f"/api/graph/setting/{setting_id}",
                params=params, headers=_settings_headers(org),
            )
        except LookupError:
            return None
        return _dict_to_resolved_setting(result)

    def add_setting(
        self, set_id, schema_revision, key, payload, *, org, state="raw",
        vault_policy_class_id=None,
    ):
        org = _resolve_client_org_arg(org)
        body = {
            "set_id": set_id,
            "schema_revision": schema_revision,
            "key": key,
            "payload": payload,
            "state": state,
        }
        if vault_policy_class_id is not None:
            body["vault_policy_class_id"] = vault_policy_class_id
        result = self._request(
            "POST", "/api/graph/setting", body=body,
            headers=_settings_headers(org),
        )
        self.last_write_report = result
        return result.get("id")

    def seal_personal_setting(self, key, value, *, policy_class_id):
        """Use the narrow personal-write seam, never generic cross-org scope.

        The caller's bearer is attached by ``_request`` for attribution. It
        does not authorize the seal; the endpoint fixes the destination and
        public-key seals immediately.
        """
        result = self._request(
            "POST",
            "/api/identity/vault-settings",
            body={
                "key": key,
                "value": value,
                "policy_class_id": policy_class_id,
            },
        )
        self.last_write_report = result
        return result.get("id")

    def override_setting(
        self, target_id, payload, *, org, state="raw",
        vault_policy_class_id=None,
    ):
        org = _resolve_client_org_arg(org)
        body = {"payload": payload, "state": state}
        if vault_policy_class_id is not None:
            body["vault_policy_class_id"] = vault_policy_class_id
        result = self._request(
            "POST", f"/api/graph/setting/{target_id}/override",
            body=body, headers=_settings_headers(org),
        )
        self.last_write_report = result
        return result.get("id")

    def exclude_setting(self, target_id, *, org, state="raw"):
        org = _resolve_client_org_arg(org)
        body = {"state": state}
        result = self._request(
            "POST", f"/api/graph/setting/{target_id}/exclude",
            body=body, headers=_settings_headers(org),
        )
        return result.get("id")

    def promote_setting(self, setting_id, to_state, *, org):
        org = _resolve_client_org_arg(org)
        self._request(
            "POST", f"/api/graph/setting/{setting_id}/promote",
            body={"to_state": to_state}, headers=_settings_headers(org),
        )

    def deprecate_setting(self, setting_id, *, org, successor_id=None):
        org = _resolve_client_org_arg(org)
        body: dict[str, Any] = {}
        if successor_id:
            body["successor_id"] = successor_id
        self._request(
            "POST", f"/api/graph/setting/{setting_id}/deprecate",
            body=body, headers=_settings_headers(org),
        )

    def remove_setting(self, setting_id, *, org):
        org = _resolve_client_org_arg(org)
        self._request(
            "DELETE", f"/api/graph/setting/{setting_id}",
            headers=_settings_headers(org),
        )

    def resolve_setting_strict(self, value, *, org):
        """Resolve a Setting by full id or id-prefix.

        Returns dict (unique match), list[dict] (ambiguous candidates),
        or None (no match). Mirrors :func:`ops.resolve_setting_strict`.
        """
        org = _resolve_client_org_arg(org)
        try:
            result = self._request(
                "GET",
                f"/api/graph/setting-resolve/{urllib.parse.quote(value, safe='')}",
                headers=_settings_headers(org),
            )
        except LookupError:
            return None
        except GraphHttpError as e:
            if e.status == 409 and isinstance(e.body, dict) and "candidates" in e.body:
                return list(e.body.get("candidates") or [])
            raise
        return result

    def chain_setting(self, set_id, key, *, org):
        """Return the supersedes chain for ``(set_id, key)``.

        ``None`` if no member resolves under the caller's scope.
        """
        org = _resolve_client_org_arg(org)
        try:
            return self._request(
                "GET",
                f"/api/graph/settings/{urllib.parse.quote(set_id, safe='')}/"
                f"{urllib.parse.quote(key, safe='')}/chain",
                headers=_settings_headers(org),
            )
        except LookupError:
            return None

    def check_setting(self, set_id, key, *, org):
        """Return schema-driven readiness findings for one Setting row."""
        findings, _satisfied = self.inspect_setting(set_id, key, org=org)
        return findings

    def inspect_setting(self, set_id, key, *, org):
        """Return failures and positive evidence from the dashboard check."""
        from .settings_ops import CheckFinding
        from .settings_ops import CheckPassed

        org = _resolve_client_org_arg(org)
        result = self._request(
            "GET",
            f"/api/graph/settings/{urllib.parse.quote(set_id, safe='')}/"
            f"{urllib.parse.quote(key, safe='')}/check",
            headers=_settings_headers(org),
        )
        return (
            [CheckFinding(**item) for item in result.get("findings", [])],
            [CheckPassed(**item) for item in result.get("satisfied", [])],
        )

    def contested_keys(self, set_id, *, org):
        """Keys of *set_id* with more than one eligible signed slot at the
        winning rung and store — the organization is contesting the value.

        Returns the ``contested`` list (metadata only, never payloads);
        empty when nothing is contested.
        """
        org = _resolve_client_org_arg(org)
        result = self._request(
            "GET",
            f"/api/graph/settings/{urllib.parse.quote(set_id, safe='')}/contested",
            headers=_settings_headers(org),
        )
        if isinstance(result, dict):
            return result.get("contested", [])
        return []

    def migrate_setting_revisions(
        self, set_id, to_rev, *, org, dry_run=False,
    ):
        from .settings_ops import MigrationReport
        org = _resolve_client_org_arg(org)
        body = {"to_rev": to_rev, "dry_run": dry_run}
        result = self._request(
            "POST", f"/api/graph/settings/{set_id}/migrate",
            body=body, headers=_settings_headers(org),
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

    def withdraw_note(self, source_id, *, org=None):
        """POST /api/graph/note/withdraw. Reversible ``deprecated`` flag flip."""
        return self._post("/api/graph/note/withdraw", {"source_id": source_id}, org=org) or {}

    def move_source(self, source_id, from_org, to_org, *, reason=None, org=None):
        body = {"from_org": from_org, "to_org": to_org}
        if reason:
            body["reason"] = reason
        return self._post(f"/api/graph/source/{source_id}/move", body, org=org) or {}

    def promote_source(self, source_id, to_state, *, org=None):
        return self._post(
            f"/api/graph/source/{source_id}/promote",
            {"to_state": to_state}, org=org,
        ) or {}

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

    def ingest_sessions(self, *, all_projects=False, project=None, force=False, session=None, org=None):
        body = {}
        if session:
            body["session"] = session
        if all_projects:
            body["all"] = True
        if project:
            body["project"] = project
        if force:
            body["force"] = True
        return self._post("/api/graph/sessions", body, org=org) or {}

    def ingest_docs(self, path, *, org=None, force=False):
        """Ingest documentation files via the dashboard (host-side, RW DB).

        Containers mount the per-org graph DBs read-only, so a direct
        ``docs-ingest`` write raises ``attempt to write a readonly
        database``. This routes the ingest through the dashboard, which runs
        it host-side against the writable DB. Mirrors :meth:`ingest_sessions`.
        """
        body = {"path": str(path)}
        if org:
            body["org"] = org
        if force:
            body["force"] = True
        return self._post("/api/graph/docs", body, org=org) or {}

    def get_dispatch_wait_status(self, bead_id):
        return self._get(f"/api/dispatch/wait/{bead_id}") or {}

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
        "short_description": result.get("short_description"),
        "keywords": result.get("keywords"),
        "org": result.get("org") or "",
        "lines": result.get("lines", lines),
        "chars": result.get("chars", len(content)),
        "content": content,
        "attachments": result.get("attachments") or [],
        "rich_content": bool(result.get("rich_content")),
        "auto_provenance": result.get("auto_provenance"),
    }


def _normalize_update_result(result: dict, content: str | None) -> dict:
    """Map server-side note-update response into the ``ops.update_note`` shape.

    Metadata-only updates pass ``content=None`` and the server omits
    ``new_version`` / ``lines`` / ``chars`` from the response. Both shapes
    round-trip cleanly here.
    """
    if not isinstance(result, dict):
        result = {}
    has_body = content is not None
    lines = (content.count("\n") + (1 if content else 0)) if has_body else 0
    chars = len(content) if has_body else 0
    return {
        "source_id": result.get("source_id"),
        "new_version": result.get("new_version"),
        "org": result.get("org") or "",
        "lines": result.get("lines", lines),
        "chars": result.get("chars", chars),
        "content": content,
        "integrated": result.get("integrated") or [],
        "not_found_comments": result.get("not_found_comments") or [],
        "attachments": result.get("attachments") or [],
        "rich_content": bool(result.get("rich_content")),
        "title": result.get("title"),
        "short_description": result.get("short_description"),
        "keywords": result.get("keywords"),
    }


def _dict_to_resolved_setting(d: dict):
    """Reconstruct a ``ResolvedSetting`` from the dashboard API response."""
    from .settings_ops import ResolvedSetting, VaultReadFailure
    # Both are absent on every ordinary member — and on a vault secret that
    # opened, which is the point: an out-of-process caller cannot tell the two
    # apart either. When one IS present the remote resolver refused, and the
    # refusal is rebuilt here so the HTTP caller branches on exactly what the
    # in-process caller branches on.
    failure = d.get("vault_error")
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
        vault_error=(
            VaultReadFailure(
                reason=failure.get("reason", ""),
                message=failure.get("message", ""),
            )
            if isinstance(failure, dict) else None
        ),
        sealed_content_key=d.get("sealed_content_key"),
    )


# ── Dispatcher ──────────────────────────────────────────────────


# Module-level switch flipped by the CLI's ``--force-host`` flag (see
# :mod:`tools.graph.cli` ``main()``). Disaster-recovery escape hatch: when
# ``True``, :func:`get_client` returns the in-process ``ops`` module instead
# of an HttpClient, bypassing the dashboard entirely.
#
# Direct ``ops.*`` writes do NOT fire ``setting.changed`` events because the
# emit hook is only registered inside the dashboard process. See the
# settings-write contract note: graph://90e6fe6d-89c. Tests opt in to direct
# mode via the ``_isolate_graph_env`` autouse fixture in
# ``tools/graph/tests/conftest.py``.
_FORCE_HOST_DIRECT = False


def get_client():
    """Return the graph client. Defaults to :class:`HttpClient` against the
    dashboard so writes fire ``setting.changed`` events through the registered
    emit hook (which only exists inside the dashboard process).

    ``GRAPH_API`` overrides the base URL (default ``https://localhost:8080``).
    ``_FORCE_HOST_DIRECT = True`` (set by the CLI's ``--force-host`` flag)
    bypasses the dashboard and returns the in-process ``ops`` module —
    disaster-recovery only, since direct writes produce no event.

    Settings-write contract: graph://90e6fe6d-89c.
    """
    if _FORCE_HOST_DIRECT:
        from . import ops
        return ops
    api = os.environ.get("GRAPH_API") or "https://localhost:8080"
    return HttpClient(api)
